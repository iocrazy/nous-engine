#!/usr/bin/env bash
# bootstrap.sh — 裸机格式化后把 nous-engine 从空系统带到全栈在跑的编排器。
#
# 设计:docs/superpowers/specs/2026-06-23-fresh-format-bootstrap-design.md
#
# 它**不取代** infra/systemd/install.sh,而是在它之前补齐前置依赖(原生 PG /
# Python·Node 依赖 / admin secret / prod 检出),
# 最后调它。复用现有脚本,自己只做编排 + 体检。
#
# 阶段(每段幂等,已就位即跳过):
#   preflight  OS/盘/驱动/CLI 体检(只读)
#   db         原生 pg17 + role/库 + (可选)从备份 restore
#   secrets    .env admin secret(缺则报缺指源,不伪造)
#   deps       后端 uv sync --extra inference
#   build      前端 npm ci + npm run build
#   checkout   (可选)派生 sibling 检出 + .nous-production 标记
#   services   调 install.sh(装单元+enginectl+sudoers+enable)+ healthz 自检
#
# 用法:
#   ./infra/bootstrap.sh --check          # 只读体检:每项 OK/缺 + 恢复指引(零改动)
#   sudo ./infra/bootstrap.sh             # 全量一键
#   sudo ./infra/bootstrap.sh --stage db  # 只跑某阶段
#   sudo ./infra/bootstrap.sh --restore <dump>   # db 段从备份恢复
#
# ⚠️ systemd unit 把检出路径写死成 nous-prod(WorkingDirectory)→ 实际 serve 的永远是
#    nous-prod。所以**全量 bootstrap 要在 nous-prod 检出内跑**(provision 的就是被 serve
#    的检出);在别的检出跑会 provision 错对象,services 段会因 nous-prod 无 venv 而拒装。
#
# 全量 run 需 root(db/services 要 apt/systemd);deps/build 以真实用户跑(venv 不落 root)。
# 机器特定 secret(DATABASE_URL 密码等)不伪造,缺则报缺指源。

set -uo pipefail

# ── 路径锚点(脚本所在 = <repo>/infra/bootstrap.sh)─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND="$REPO_ROOT/backend"
FRONTEND="$REPO_ROOT/frontend"
ENV_FILE="$BACKEND/.env"
PROD_CHECKOUT="$(cd "$REPO_ROOT/.." && pwd)/nous-prod"

# ── 颜色 / 计数 ─────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_OK=$'\033[1;32m'; C_MISS=$'\033[1;31m'; C_WARN=$'\033[1;33m'
  C_MAN=$'\033[1;36m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_OK=""; C_MISS=""; C_WARN=""; C_MAN=""; C_DIM=""; C_RST=""
fi

N_OK=0; N_MISS=0; N_MANUAL=0; N_WARN=0
declare -a TODO=()

ok()     { printf '  %s✓%s %s\n' "$C_OK" "$C_RST" "$1"; N_OK=$((N_OK+1)); }
miss()   { printf '  %s✗%s %s\n' "$C_MISS" "$C_RST" "$1"; N_MISS=$((N_MISS+1));
           [[ -n "${2:-}" ]] && { printf '      %s↳ %s%s\n' "$C_DIM" "$2" "$C_RST"; TODO+=("$1 — $2"); } || TODO+=("$1"); }
manual() { printf '  %s⚙%s %s\n' "$C_MAN" "$C_RST" "$1"; N_MANUAL=$((N_MANUAL+1));
           [[ -n "${2:-}" ]] && { printf '      %s↳ %s%s\n' "$C_DIM" "$2" "$C_RST"; TODO+=("[人工] $1 — $2"); } || TODO+=("[人工] $1"); }
warn()   { printf '  %s!%s %s\n' "$C_WARN" "$C_RST" "$1"; N_WARN=$((N_WARN+1)); }
section(){ printf '\n%s━━ %s%s\n' "$C_DIM" "$1" "$C_RST"; }

have()   { command -v "$1" >/dev/null 2>&1; }
log()    { printf '%s==>%s %s\n' "$C_MAN" "$C_RST" "$1"; }
die()    { printf '%sERROR:%s %s\n' "$C_MISS" "$C_RST" "$1" >&2; exit 1; }

# 变更类阶段守卫:需 root(apt / systemd / sudoers / pg 超管)。
require_root() { [[ ${EUID} -eq 0 ]] || die "阶段 '$1' 需 root:sudo $0 --stage $1"; }

# 真实用户(sudo 场景取 SUDO_USER,否则当前)。venv / npm / git 必须以它跑,
# 不能落 root 所有权。
REAL_USER="${SUDO_USER:-$(id -un)}"
# 真实用户家目录(sudo 下 $HOME=/root,但 venv/检出属主要用真实用户)。
USER_HOME="$(getent passwd "$REAL_USER" 2>/dev/null | cut -d: -f6)"; USER_HOME="${USER_HOME:-$HOME}"
as_user() {
  # 以 REAL_USER 跑一条命令(带登录环境,确保 uv/npm/node 在 PATH)。
  if [[ ${EUID} -eq 0 && -n "${SUDO_USER:-}" ]]; then
    sudo -u "$SUDO_USER" -H bash -lc "$1"
  else
    bash -lc "$1"
  fi
}

# DATABASE_URL(postgresql+asyncpg://user:pw@host:port/db)→ 解析出 host/port/db/user。
# 不打印密码。
_db_field() {
  local url; url="$(grep -E '^DATABASE_URL=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
  [[ -z "$url" ]] && return 1
  # 去掉 scheme + 凭证,剩 host:port/db
  local rest="${url#*@}"
  case "$1" in
    host) echo "${rest%%:*}";;
    port) rest="${rest#*:}"; echo "${rest%%/*}";;
    db)   echo "${rest##*/}";;
    user) local cred="${url#*://}"; cred="${cred%%@*}"; echo "${cred%%:*}";;
    pass) local cred="${url#*://}"; cred="${cred%%@*}"; echo "${cred#*:}";;
  esac
}

# ── preflight:OS / 盘 / 驱动 / CLI(纯只读)───────────────────────────────
check_preflight() {
  section "preflight — OS / 磁盘 / GPU 驱动 / CLI 工具"

  if have systemctl; then ok "systemd 在位"; else miss "无 systemd" "本栈靠 systemd 托管,需要 systemd 系统"; fi

  # 磁盘:系统盘(pg 数据落这)+ 大盘(模型/备份)
  local sys_avail prog_avail
  sys_avail="$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')"
  [[ -n "$sys_avail" ]] && { if (( sys_avail >= 50 )); then ok "系统盘 / 余 ${sys_avail}G"; else warn "系统盘 / 仅余 ${sys_avail}G(pg 数据落此,建议 >50G)"; fi; }
  if [[ -d /media/heygo/program ]]; then
    prog_avail="$(df -BG --output=avail /media/heygo/program 2>/dev/null | tail -1 | tr -dc '0-9')"
    ok "大盘 /media/heygo/program 已挂(余 ${prog_avail:-?}G,放模型/备份)"
  else
    miss "大盘 /media/heygo/program 未挂" "模型权重 + DB 备份落此盘,挂载后重跑"
  fi

  # GPU 驱动
  if have nvidia-smi; then
    local n; n="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | wc -l)"
    if (( n > 0 )); then ok "NVIDIA 驱动在位($n 张卡)"; else warn "nvidia-smi 在但查不到卡(驱动/GSP 状态?)"; fi
  else
    miss "无 nvidia-smi" "装 NVIDIA 驱动(OS 层,bootstrap 不代装)"
  fi

  # 必需 CLI
  local cli; for cli in git curl openssl uv node npm; do
    if have "$cli"; then ok "CLI: $cli"; else miss "缺 CLI: $cli" "先装 $cli"; fi
  done
  # pg 客户端在 db 阶段细查,这里只提示
  have psql        || warn "psql 未在 PATH(db 阶段需要 postgresql-client-17)"
}

# ── db:原生 pg + 库可连 ─────────────────────────────────────────────────
check_db() {
  section "db — 原生 PostgreSQL + 库连通"

  if systemctl is-active --quiet postgresql 2>/dev/null; then
    ok "postgresql.service active(原生 systemd 托管)"
  elif have pg_lsclusters && pg_lsclusters 2>/dev/null | grep -q online; then
    warn "原生 pg cluster 在线但 postgresql.service 未 active(sudo systemctl enable --now postgresql)"
  else
    miss "未装/未起原生 postgresql" "apt 装 postgresql-17(见 native-pg spec);bootstrap PR-2 将自动化"
  fi

  [[ -f "$ENV_FILE" ]] || { miss "无 $ENV_FILE → 无法取 DATABASE_URL" "见 secrets 阶段"; return; }
  local host port db user
  host="$(_db_field host)"; port="$(_db_field port)"; db="$(_db_field db)"; user="$(_db_field user)"
  if [[ -z "$host" ]]; then miss "DATABASE_URL 解析失败" "检查 $ENV_FILE 的 DATABASE_URL"; return; fi

  if have pg_isready && pg_isready -h "$host" -p "$port" -q 2>/dev/null; then
    ok "pg 可达 $host:$port"
    # 库 + 表存在?用解析出的密码(PGPASSWORD,不回显)连;失败不致命。
    if have psql; then
      local ntab pw
      pw="$(_db_field pass)"
      ntab="$(PGCONNECT_TIMEOUT=4 PGPASSWORD="$pw" psql -h "$host" -p "$port" -U "$user" -d "$db" -tAc \
              "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null | tr -dc '0-9')"
      if [[ -n "$ntab" ]] && (( ntab > 0 )); then ok "库 $db 可连(public 下 $ntab 张表)";
      elif [[ -n "$ntab" ]]; then warn "库 $db 可连但空表(backend 首启会 create_all 自建)";
      else warn "库 $db 连接失败(密码/权限?此处 psql 仅尽力探测,backend 用 asyncpg)"; fi
    fi
  else
    miss "pg 不可达 $host:$port" "确认 pg 起着 + 监听该端口"
  fi
}

# ── secrets:admin secret(不伪造)───────────────────────────────────────
check_secrets() {
  section "secrets — admin 凭证"

  if [[ -f "$ENV_FILE" ]]; then
    ok "$ENV_FILE 存在"
    local k; for k in ADMIN_PASSWORD ADMIN_SESSION_SECRET DATABASE_URL; do
      if grep -qE "^$k=.+" "$ENV_FILE"; then ok ".env: $k 已设"; else miss ".env 缺 $k" "./infra/security/gen-admin-secrets.sh 生成后填入"; fi
    done
    grep -qE '^ADMIN_TOKEN=.+' "$ENV_FILE" || warn ".env 无 ADMIN_TOKEN(CLI bearer,可选;浏览器登录用 ADMIN_PASSWORD)"
  else
    miss "无 $ENV_FILE" "cp 一份或 ./infra/security/gen-admin-secrets.sh >> backend/.env(填 DATABASE_URL 等)"
  fi
}

# ── deps:后端 venv(含 vllm)────────────────────────────────────────────
check_deps() {
  section "deps — 后端 venv(inference)"

  local bpy="$BACKEND/.venv/bin/python"
  if [[ -x "$bpy" ]]; then
    if "$bpy" -c 'import vllm' 2>/dev/null; then ok "后端 venv 有 vllm(--extra inference 已装)";
    else miss "后端 venv 缺 vllm" "cd backend && uv sync --extra inference(漏 --extra 会常驻 0/N)"; fi
  else
    miss "无后端 venv($BACKEND/.venv)" "cd backend && uv sync --extra inference"
  fi

  # 不再检查 aligner venv —— ForcedAligner 微服务已退役并删除(见 do_deps 注释)。
}

# ── build:前端依赖 + dist ───────────────────────────────────────────────
check_build() {
  section "build — 前端依赖 + dist 产物"
  [[ -d "$FRONTEND/node_modules" ]] && ok "前端 node_modules 已装" || miss "前端无 node_modules" "cd frontend && npm ci"
  if [[ -f "$FRONTEND/dist/index.html" ]]; then ok "前端 dist 已构建(backend serve 它)"; else miss "前端无 dist" "cd frontend && npm run build"; fi
}

# ── checkout:专用 prod 检出 ────────────────────────────────────────────
check_checkout() {
  section "checkout — 专用生产检出(systemd 指它)"
  if [[ -d "$PROD_CHECKOUT/.git" || -f "$PROD_CHECKOUT/.git" ]]; then
    ok "prod 检出存在($PROD_CHECKOUT)"
    [[ -f "$PROD_CHECKOUT/.nous-production" ]] && ok ".nous-production 标记在位(deploy 凭它放行)" || miss "prod 检出缺 .nous-production 标记" "touch $PROD_CHECKOUT/.nous-production"
    [[ -L "$PROD_CHECKOUT/backend/.env" ]] && ok "prod .env symlink → 单一来源" || warn "prod backend/.env 非 symlink(应 ln -s 到 nous-engine/backend/.env)"
  else
    manual "无专用 prod 检出($PROD_CHECKOUT)" "见 infra/PROD_CHECKOUT.md 一次性搭建序列"
  fi
}

# ── services:systemd 单元 + 健康 ──────────────────────────────────────
check_services() {
  section "services — systemd 单元 + 健康"
  local svc; for svc in nous-engine-backend nous-engine-status; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then ok "$svc active"; else miss "$svc 未运行" "sudo ./infra/systemd/install.sh && sudo systemctl start $svc"; fi
  done
  local tmr; for tmr in nous-engine-healthprobe.timer nous-engine-dbbackup.timer; do
    systemctl is-active --quiet "$tmr" 2>/dev/null && ok "$tmr active" || warn "$tmr 未启用"
  done
  have enginectl && ok "enginectl 在 PATH" || warn "enginectl 未装(install.sh 会装到 /usr/local/bin)"
  if have curl; then
    curl -fsS --noproxy '*' -m 5 http://127.0.0.1:8000/healthz >/dev/null 2>&1 && ok "本机 /healthz 200" || warn "本机 /healthz 不通(backend 未起?)"
  fi
}

# ── 汇总 ────────────────────────────────────────────────────────────────
summary() {
  printf '\n%s━━ 汇总 %s\n' "$C_DIM" "$C_RST"
  printf '  %s%d OK%s   %s%d 缺%s   %s%d 需人工%s   %s%d 警告%s\n' \
    "$C_OK" "$N_OK" "$C_RST" "$C_MISS" "$N_MISS" "$C_RST" \
    "$C_MAN" "$N_MANUAL" "$C_RST" "$C_WARN" "$N_WARN" "$C_RST"
  if (( N_MISS > 0 || N_MANUAL > 0 )); then
    printf '\n  待办:\n'
    local t; for t in "${TODO[@]}"; do printf '   • %s\n' "$t"; done
  else
    printf '\n  %s全部就位 — 全栈应可一键起。%s\n' "$C_OK" "$C_RST"
  fi
}

run_check() {
  printf '%snous-engine bootstrap --check%s  (只读体检,零改动)\n' "$C_MAN" "$C_RST"
  printf '%srepo: %s%s\n' "$C_DIM" "$REPO_ROOT" "$C_RST"
  check_preflight; check_db; check_secrets; check_deps; check_build; check_checkout; check_services
  summary
  (( N_MISS > 0 )) && return 1 || return 0
}

# ═══ 变更类阶段(PR-2:db / deps / build)═══════════════════════════════════
# 每段先判定目标态,已就位即跳过 → 幂等 + 断点续跑。

# PGDG 源(Ubuntu 24.04 自带 pg16,要 17 需加 apt.postgresql.org)。
install_pgdg_repo() {
  [[ -f /etc/apt/sources.list.d/pgdg.list ]] && return 0
  log "加 PGDG apt 源(apt.postgresql.org)"
  . /etc/os-release
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc || die "下载 PGDG key 失败"
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
}

# 建 role + 库(与 DATABASE_URL 完全一致;幂等)。pw 是 base64 url-safe 无引号。
ensure_role_and_db() {
  local user pass db
  user="$(_db_field user)"; pass="$(_db_field pass)"; db="$(_db_field db)"
  [[ -n "$user" && -n "$db" ]] || die "DATABASE_URL 解析不出 user/db,无法建库"
  if sudo -u postgres psql -tAc "select 1 from pg_roles where rolname='$user'" 2>/dev/null | grep -q 1; then
    ok "role $user 已存在"
  else
    sudo -u postgres psql -qc "CREATE ROLE \"$user\" LOGIN PASSWORD '$pass'" || die "建 role 失败"
    ok "建 role $user"
  fi
  if sudo -u postgres psql -tAc "select 1 from pg_database where datname='$db'" 2>/dev/null | grep -q 1; then
    ok "库 $db 已存在"
  else
    sudo -u postgres createdb -O "$user" "$db" || die "建库失败"
    ok "建库 $db(owner=$user)"
  fi
}

do_db() {
  require_root db
  section "db — 原生 pg17 + role/库"
  [[ -f "$ENV_FILE" ]] || die "无 $ENV_FILE,先跑 secrets 阶段(PR-3)或手动建 .env 填 DATABASE_URL"
  if systemctl is-active --quiet postgresql 2>/dev/null; then
    ok "postgresql 已运行 — 跳过安装"
  else
    have apt-get || die "非 apt 系发行版,db 阶段不支持(手动装 pg17)"
    apt-cache show postgresql-17 >/dev/null 2>&1 || install_pgdg_repo
    log "apt 装 postgresql-17 + client"
    apt-get update -y && apt-get install -y postgresql-17 postgresql-client-17 || die "apt 装 pg 失败"
    systemctl enable --now postgresql
    ok "postgresql 已装并启用"
  fi
  ensure_role_and_db
  if [[ -n "${RESTORE_DUMP:-}" ]]; then
    [[ -f "$RESTORE_DUMP" ]] || die "--restore 文件不存在: $RESTORE_DUMP"
    local host port db; host="$(_db_field host)"; port="$(_db_field port)"; db="$(_db_field db)"
    log "从 $RESTORE_DUMP 恢复到 $db(pg_restore)"
    PGPASSWORD="$(_db_field pass)" pg_restore -h "$host" -p "$port" -U "$(_db_field user)" \
      -d "$db" --no-owner --clean --if-exists "$RESTORE_DUMP" || warn "pg_restore 有非致命报错(看上面)"
    ok "恢复完成"
  else
    log "未传 --restore → 建空库,backend 首启 create_all 自建 schema(与现状一致)"
  fi
}

do_deps() {
  section "deps — 后端 venv(inference)"
  [[ ${EUID} -eq 0 && -z "${SUDO_USER:-}" ]] && die "deps 不要以纯 root 跑(venv 会落 root 所有);用普通用户或 sudo(带 SUDO_USER)"
  local bpy="$BACKEND/.venv/bin/python"
  if [[ -x "$bpy" ]] && "$bpy" -c 'import vllm' 2>/dev/null; then
    ok "后端 venv 已含 vllm — 跳过"
  else
    log "后端 uv sync --extra inference(~分钟级,装 vllm/torch)"
    as_user "cd '$BACKEND' && uv sync --extra inference" || die "uv sync 失败"
    ok "后端依赖就位"
  fi
  # 不再建 aligner venv —— ForcedAligner 微服务已退役(MOSS 内建时间戳/说话人分离);
  # 2026-07-30 单元、源码、模型、venv 已全部删除,bootstrap 无需再管它。
}

# dist 是否比最新源文件新(借 deploy 的时间戳思路,避免无谓重 build)。
_dist_fresh() {
  local dist="$FRONTEND/dist/index.html"
  [[ -f "$dist" ]] || return 1
  local newest
  newest="$(find "$FRONTEND/src" "$FRONTEND/index.html" "$FRONTEND/package.json" \
            -type f -newer "$dist" -print -quit 2>/dev/null)"
  [[ -z "$newest" ]]
}

do_build() {
  section "build — 前端依赖 + dist"
  [[ ${EUID} -eq 0 && -z "${SUDO_USER:-}" ]] && die "build 不要以纯 root 跑;用普通用户或 sudo"
  if [[ -d "$FRONTEND/node_modules" ]]; then
    ok "node_modules 已装 — 跳过 npm ci"
  else
    log "npm ci"
    as_user "cd '$FRONTEND' && npm ci" || die "npm ci 失败"
  fi
  if _dist_fresh; then
    ok "dist 比源新 — 跳过 build"
  else
    log "npm run build"
    as_user "cd '$FRONTEND' && npm run build" || die "前端 build 失败"
    ok "dist 已构建"
  fi
}

# ═══ 变更类阶段(PR-3:secrets / checkout / services)══════════════════════

ENV_FRESH=0   # do_secrets 新建 .env(含占位)时置 1 → 全量 run 在 db 前停下让人填

do_secrets() {
  section "secrets — admin 凭证"
  # .env:缺则从 .env.example 起一份(机器特定值需人工填)
  if [[ ! -f "$ENV_FILE" ]]; then
    [[ -f "$BACKEND/.env.example" ]] || die "无 $ENV_FILE 且无 .env.example,无法生成"
    log "从 .env.example 起 backend/.env"
    as_user "cp '$BACKEND/.env.example' '$ENV_FILE'"
    ENV_FRESH=1
    manual ".env 由模板生成,需人工填机器特定值" "编辑 $ENV_FILE:DATABASE_URL 密码 / MODELS_ROOT 等,再重跑"
  else
    ok ".env 已存在"
  fi
  # admin secret:缺哪个补哪个(gen-admin-secrets 生成;只追加缺的,绝不覆盖已有)
  local need=()
  grep -qE '^ADMIN_PASSWORD=.+'        "$ENV_FILE" || need+=(ADMIN_PASSWORD)
  grep -qE '^ADMIN_SESSION_SECRET=.+'  "$ENV_FILE" || need+=(ADMIN_SESSION_SECRET)
  if (( ${#need[@]} )); then
    log "生成并追加缺失 admin secret: ${need[*]}"
    local gen; gen="$(as_user "'$SCRIPT_DIR/security/gen-admin-secrets.sh'")" || die "gen-admin-secrets 失败"
    local k; for k in "${need[@]}"; do
      printf '%s\n' "$gen" | grep -E "^$k=" >> "$ENV_FILE"
    done
    ok "已追加 admin secret: ${need[*]}"
  else
    ok "admin secret 已齐"
  fi
}

# 专用生产检出:从当前(dev)检出派生 worktree。注意 venv/dist 是 per-checkout 的
# gitignore 物,prod 检出建好后须在其内部再跑一遍 deps/build(即在 prod 内重跑
# bootstrap)。本阶段只负责「建出 + 标记 + .env 单一来源」。
do_checkout() {
  section "checkout — 专用生产检出(systemd 指它)"
  if [[ -e "$PROD_CHECKOUT/.git" ]]; then
    ok "prod 检出已存在 — 跳过"
  else
    log "建 prod 检出:git worktree add --detach $PROD_CHECKOUT origin/master"
    as_user "cd '$REPO_ROOT' && git fetch origin -q && git worktree add --detach '$PROD_CHECKOUT' origin/master" \
      || die "建 worktree 失败"
    ok "prod 检出建好($PROD_CHECKOUT)"
    warn "prod 检出的 venv/dist 需在其内部 provision:cd $PROD_CHECKOUT && sudo ./infra/bootstrap.sh"
  fi
  [[ -f "$PROD_CHECKOUT/.nous-production" ]] || { as_user "touch '$PROD_CHECKOUT/.nous-production'"; ok "打 .nous-production 标记"; }
  if [[ ! -e "$PROD_CHECKOUT/backend/.env" ]]; then
    as_user "ln -s '$ENV_FILE' '$PROD_CHECKOUT/backend/.env'"; ok "prod backend/.env symlink → 单一来源"
  else
    ok "prod backend/.env 已在位"
  fi
}

do_services() {
  require_root services
  section "services — systemd 单元 + 启停 + 自检"
  [[ -x "$SCRIPT_DIR/systemd/install.sh" ]] || die "缺 $SCRIPT_DIR/systemd/install.sh"
  # systemd unit 把检出路径写死(WorkingDirectory)→ 实际 serve 的检出未必是当前检出。
  # 装服务前确认那个检出已 provision(有 venv),否则会装出起不来的服务。
  local served_repo
  served_repo="$(grep -m1 '^WorkingDirectory=' "$SCRIPT_DIR/systemd/nous-engine-backend.service" 2>/dev/null | cut -d= -f2)"
  served_repo="${served_repo%/backend}"
  if [[ -n "$served_repo" && ! -x "$served_repo/backend/.venv/bin/python" ]]; then
    die "systemd 指向的检出未 provision($served_repo 无 backend/.venv)。请在该检出内跑:cd $served_repo && sudo ./infra/bootstrap.sh"
  fi
  log "调 install.sh(装单元 + enginectl + sudoers + enable --now)"
  "$SCRIPT_DIR/systemd/install.sh" install >/dev/null || die "install.sh 失败"
  ok "systemd 单元已装并启用"
  # 自检:本机 healthz(backend + vLLM 起来要几秒,重试)
  local i hit=0
  for i in 1 2 3 4 5 6; do
    if curl -fsS --noproxy '*' -m 5 http://127.0.0.1:8000/healthz >/dev/null 2>&1; then hit=1; break; fi
    sleep 3
  done
  (( hit )) && ok "本机 /healthz 200" || warn "本机 /healthz 暂不通(backend 还在起?journalctl -u nous-engine-backend -f)"
}

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ── 参数解析(支持 --restore <dump> 给 db 阶段)─────────────────────────
RESTORE_DUMP=""
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --restore) RESTORE_DUMP="${2:-}"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]:-}"

run_stage() {
  case "$1" in
    preflight) check_preflight; summary ;;
    secrets)  do_secrets ;;
    db)       do_db ;;
    deps)     do_deps ;;
    build)    do_build ;;
    checkout) do_checkout ;;
    services) do_services ;;
    *) echo "未知阶段: ${1:-<空>}(preflight|secrets|db|deps|build|checkout|services)" >&2; exit 2 ;;
  esac
}

# ── 入口 ────────────────────────────────────────────────────────────────
case "${1:-}" in
  --check|check) run_check ;;
  --stage) run_stage "${2:-}" ;;
  -h|--help|help) usage ;;
  "") # 默认全量:secrets → db → deps → build → checkout → services → 体检。
      # 每段幂等(已就位即跳过)。需 root(db/services)。
      require_root "(全量)"
      log "bootstrap 全量:secrets → db → deps → build → checkout → services"
      do_secrets
      if (( ENV_FRESH )); then
        printf '\n%s停%s:.env 刚由模板生成,DATABASE_URL 等机器特定值需先人工填,再重跑 bootstrap。\n' "$C_WARN" "$C_RST"
        printf '编辑 %s 后:sudo %s\n' "$ENV_FILE" "$0"
        exit 3
      fi
      do_db; do_deps; do_build; do_checkout; do_services
      printf '\n'
      run_check; rc=$?
      exit $rc ;;
  *) echo "未知参数: $1" >&2; usage; exit 2 ;;
esac
