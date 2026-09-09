#!/usr/bin/env bash
# nous-engine 一键上线(2026-06-19)。
#
#   ./infra/deploy.sh
#
# 流程:拉 master → 前端 build → 重启后端 → 自检。每步 fail-loud,**绝不静默**——
# 杜绝「build 没成 / dist 没更新,却以为上线了」的乌龙(2026-06-19 矩阵那次踩到:
# 后端重启了但 dist 停在旧版,页面没变,来回好几趟才发现)。
#
# 为什么需要它:这台机器**没有自动部署** —— 后端 serve 编译好的 frontend/dist(不是源码),
# 且作为常驻进程跑;改了得 ① 前端 build ② 重启后端 才生效。本脚本把这两步 + 拉代码 + 自检
# 收成一条命令。
#
# 以 heygo 身份跑(git/npm 不能用 root,否则在仓库里造 root 属主文件);只有重启那步用 sudo
# (会提示输密码,除非配了 NOPASSWD)。
#
# ⚠️ 重启后端会**卸掉所有已加载模型**,vLLM ~30s 重载 —— 生产高峰期慎发版。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# --force:允许覆盖脏工作树(默认拒绝,见下面的守卫)。
FORCE=0
for a in "$@"; do case "$a" in --force) FORCE=1 ;; *) echo "未知参数 '$a'(只认 --force)" >&2; exit 2 ;; esac; done
LOCAL="${NOUS_LOCAL_URL:-http://127.0.0.1:8000}"

if [[ "${EUID}" -eq 0 ]]; then
  echo "ERROR: 别用 root/sudo 跑整个脚本(会在仓库造 root 属主文件)。直接 ./infra/deploy.sh,只有重启那步会自己提权。" >&2
  exit 1
fi

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✅ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m❌ %s\033[0m\n' "$*" >&2; exit 1; }

cd "$REPO"

# ---------- 0. 非交互 ssh 下的 PATH 自举(2026-09-09 ship.sh 首跑踩到)----------
# ship.sh 经 `ssh host './infra/deploy.sh'` 调本脚本:非登录、非交互 shell,.zshrc 不读 ——
# uv(linuxbrew)和 node(nvm 的 shell 函数)都不在 PATH 上,`uv: command not found`;
# 而这时 git 已经 reset 到新版,venv/dist/重启一步没做(#736 首跑就是这个半截状态:
# 检出是新代码,进程还是旧的)。所以本脚本自己把路铺好,不依赖调用它的 shell 是谁:
# brew shellenv → nvm.sh(有 default 别名时它的 node 优先,与手动发版一致)→ ~/.local/bin。
# 缺哪个就跳过,下面 uv/npm 找不到照旧 fail-loud。
for brew in /home/linuxbrew/.linuxbrew/bin/brew /opt/homebrew/bin/brew; do
  [[ -x "$brew" ]] && { eval "$("$brew" shellenv)"; break; }
done
if [[ -s "$HOME/.nvm/nvm.sh" ]]; then
  export NVM_DIR="$HOME/.nvm"
  set +u; . "$NVM_DIR/nvm.sh" >/dev/null 2>&1 || true; set -u   # nvm.sh 在 set -u 下会报未定义变量
fi
export PATH="$HOME/.local/bin:$PATH"

# ---------- 1. 同步 master(只在专用生产检出)----------
# 生产与 dev 检出分离(2026-06-21):systemd 指向专用 nous-prod 检出,dev session 各用
# 自己的 worktree,谁都不碰生产。生产检出 detached、只读、deploy 专用 → fetch + reset
# --hard 同步到 origin/master(gitignore 的 .env/dist/node_modules 不受影响)。
# .nous-production 标记防呆:dev 检出误跑 deploy.sh 会被 reset --hard 卷走未提交改动 → 直接拒绝。
step "同步到 origin/master"
[[ -f "$REPO/.nous-production" ]] || die "不在专用生产检出(缺 .nous-production 标记)。生产部署只在 nous-prod 跑;dev 改动走 PR→CI→master 后,在 nous-prod 里 ./infra/deploy.sh。"
git fetch origin master -q || die "git fetch 失败。"
# 脏树守卫(2026-09-07 安全审查):`reset --hard` 会**不吭一声**丢掉生产机上的本地改动
# (就地调过的 models.d/*.yaml 之类)。本仓库别处都是 fail-loud(dist 时间戳校验、
# `import vllm` 校验),这里也一样:先报出将丢什么,要覆盖得显式 --force。
dirty="$(git status --porcelain --untracked-files=no)"
if [[ -n "$dirty" ]]; then
  if (( FORCE )); then
    printf '\033[33m⚠ --force:以下本地改动将被 reset --hard 丢弃\033[0m\n%s\n' "$dirty" >&2
  else
    printf '%s\n' "$dirty" >&2
    die "生产检出有未提交改动(上面这些),reset --hard 会丢掉它们。先提交/推走,或确认要丢就 ./infra/deploy.sh --force。"
  fi
fi
git reset --hard origin/master || die "git reset --hard origin/master 失败。"
ok "已同步到 $(git rev-parse --short HEAD)"

# ---------- 1b. 后端 venv 同步(必带 --extra inference)----------
# 2026-06-21 真机踩:专用生产检出 nous-prod 的 .venv 若用裸 `uv sync` 建,只装 API server
# 包(fastapi/sqlalchemy),**缺 vllm/torch/safetensors/transformers** → backend 能起、API 能跑,
# 但常驻模型 spawn vLLM 时 `ModuleNotFoundError: No module named 'vllm'` → 全失败、UI 卡
# 「系统启动中 · 模型加载 0/N」。每次发版都同步 venv:既让 venv 跟代码一致(拉到的新依赖),
# 又确保 inference extra 一直在。uv 在 venv 已最新时几乎瞬返(只对账 lock),发版常态零成本。
step "后端 venv 同步(--extra inference)"
cd "$REPO/backend"
uv sync --extra inference || die "uv sync --extra inference 失败 —— venv 没对齐,中止(否则常驻模型会 0/N)。"
uv run python -c 'import vllm' 2>/dev/null || die "venv 同步后仍 import vllm 失败 —— inference extra 没装上,中止(否则常驻模型 spawn vLLM 会崩)。"
ok "venv 已同步且 vllm 可导入"
cd "$REPO"

# ---------- 2. 前端 build(fail-loud + dist 时间戳校验)----------
step "前端 build"
build_start=$(date +%s)
cd "$REPO/frontend"
if npm run build; then
  ok "npm run build 完成"
elif [[ -d src/wasm/pkg ]]; then
  # prebuild(wasm-pack)常因缺 rust 工具链挂;wasm pkg 已存在时回退直接 tsc+vite。
  echo ">> npm run build 失败,回退 tsc -b && vite build(复用现有 wasm pkg)"
  ./node_modules/.bin/tsc -b && ./node_modules/.bin/vite build || die "前端 build 失败(tsc/vite)。"
  ok "回退 build 完成"
else
  die "前端 build 失败,且无 src/wasm/pkg 可回退 —— 先 npm run wasm:build。"
fi
# 关键防呆:dist 必须真被这次 build 写新(否则就是『以为上了、其实没动』)。
dist_mtime=$(stat -c %Y "$REPO/frontend/dist/index.html" 2>/dev/null || echo 0)
(( dist_mtime >= build_start )) || die "frontend/dist 没更新(mtime 早于 build 开始)—— build 没真正产出,中止,不重启。"
ok "dist 已更新($(date -d @"$dist_mtime" '+%H:%M:%S'))"
cd "$REPO"

# ---------- 3. 重启后端(sudo)----------
# --no-block:阻塞式 systemctl restart 客户端在本机会傻等 job-done 不返回(实测挂 48min,
# 但 unit ~3s 就重启完、active)。入队即返回,下面第 4 步 poll 等新实例真就绪。
#
# 重启前抓 InvocationID(每次启动唯一)—— 第 4 步要确认它**变了**才算新实例起来。
# 否则:--no-block restart 是异步的,停掉带 vLLM 的重后端要几十秒~分钟,这期间**旧进程
# 还在 answer /healthz 200** → 只 poll /healthz 会对着旧进程误判「上线成功」,新代码其实
# 没生效(2026-06-21 #590 部署真踩到:自检过了但还在跑旧代码)。
prev_inv=$(systemctl show -p InvocationID --value nous-engine-backend 2>/dev/null || echo "")
step "重启 nous-engine-backend(卸全模型,vLLM ~30s 重载)"
sudo systemctl --no-block restart nous-engine-backend || die "systemctl restart 入队失败。"
ok "已发出重启(--no-block,等新实例起来)"

# ---------- 4. 自检:等**新实例**(InvocationID 变 + active + /healthz 200)----------
# 三者齐备才算真上线 —— InvocationID != prev 排除「旧进程answering」的假成功。
step "自检(确认新实例,不是旧进程)"
new_up=0; inv=""; act=""; code=""
for i in $(seq 1 90); do
  inv=$(systemctl show -p InvocationID --value nous-engine-backend 2>/dev/null || echo "")
  act=$(systemctl is-active nous-engine-backend 2>/dev/null || echo "")
  if [[ -n "$inv" && "$inv" != "$prev_inv" && "$act" == "active" ]]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' --max-time 5 "$LOCAL/healthz" 2>/dev/null || echo 000)
    [[ "$code" == "200" ]] && { ok "新实例健康(InvocationID 已换 + /healthz 200,${i}×2s)"; new_up=1; break; }
  fi
  sleep 2
done
[[ $new_up -eq 1 ]] || die "新实例 180s 内未就绪(InvocationID=$inv prev=$prev_inv active=$act /healthz=$code)。查 journalctl -u nous-engine-backend -n 50。"

printf '\n\033[1;32m✅ 上线完成 — %s\033[0m\n' "$(git rev-parse --short HEAD)"
