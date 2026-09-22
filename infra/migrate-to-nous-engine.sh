#!/usr/bin/env bash
# migrate-to-nous-engine.sh — 仓库/服务整体改名 nous-center → nous-engine 的**执行**脚本。
#
#   sudo ./infra/migrate-to-nous-engine.sh [--cloudflared /path/to/cloudflared] [--dry-run]
#
# 背景:另一产品(mediahub)改名 nous,本仓库(推理算力层)改名 nous-engine 以消歧,
# 并把裸 `nous-` 前缀让给新平台 —— systemd 单元全套 nous-* → nous-engine-*,
# CLI nousctl → enginectl,目录 /repos/nous-center → /repos/nous-engine。
# 配套 runbook:MIGRATION-nous-engine.md(必读:含前置检查/验证/回滚/GitHub 收尾)。
#
# ⚠️⚠️ 自吃尾巴的坑:本脚本执行途中会 `mv` 掉自己所在的仓库目录(步骤⑤)。shell 若边读
#      边执行,mv 后再读后续行会读到已不存在的路径 → 脚本半截崩。为此**全部逻辑包在
#      main() 里,文件最后一行才 `main "$@"`** —— bash 要执行最后一行必先读完整个文件,
#      main() 函数体在到达调用点前已整体解析进内存,mv 掉磁盘上的源文件也不影响执行。
#      **改本脚本时务必保持这个结构**,别把可执行语句挪到 main() 外的顶层。
#
# 幂等/失败即停:set -euo pipefail;每步失败打印明确位置退出,绝不半途静默。
set -euo pipefail

OLD_DIR="/media/heygo/program/projects-code/repos/nous-center"
NEW_DIR="/media/heygo/program/projects-code/repos/nous-engine"
UNIT_DIR="/etc/systemd/system"
ENGINECTL_DST="/usr/local/bin/enginectl"
RUN_USER="heygo"

# 迁移后由本脚本纳管的**新**单元源文件名(与 infra/systemd/install.sh 的 UNIT_FILES 一致)。
NEW_UNIT_FILES=(
  nous-engine-backend.service nous-engine-cloudflared.service nous-engine-status.service
  nous-engine-aligner.service nous-engine-healthprobe.service nous-engine-healthprobe.timer
  nous-engine-dbbackup.service nous-engine-dbbackup.timer nous-engine.target
)

# 迁移前要**记录 enabled 状态**并在迁移后按原状恢复的旧单元(不硬编码 enable 列表——
# cloudflared/aligner 实际是 disabled、moss-asr/gpu-guard 根本没装,逐个 is-enabled 探真值)。
# 每项 "旧单元:新单元"。
declare -A OLD2NEW=(
  [nous-backend.service]=nous-engine-backend.service
  [nous-cloudflared.service]=nous-engine-cloudflared.service
  [nous-status.service]=nous-engine-status.service
  [nous-aligner.service]=nous-engine-aligner.service
  [nous-healthprobe.timer]=nous-engine-healthprobe.timer
  [nous-dbbackup.timer]=nous-engine-dbbackup.timer
  [nous.target]=nous-engine.target
)
# 记录顺序(bash 关联数组无序;显式列表保证 enable 恢复顺序稳定:先 backend 再其余)。
OLD_UNIT_ORDER=(
  nous-backend.service nous-status.service nous-aligner.service
  nous-healthprobe.timer nous-dbbackup.timer nous-cloudflared.service nous.target
)

DRY_RUN=0
CF_SRC=""   # --cloudflared 指定的二进制路径(可选)

# ── 样式/日志 ──────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then B=$'\033[1m'; GRN=$'\033[1;32m'; RED=$'\033[1;31m'; YEL=$'\033[1;33m'; CYN=$'\033[1;36m'; RST=$'\033[0m'
else B=""; GRN=""; RED=""; YEL=""; CYN=""; RST=""; fi
step() { printf '\n%s▸ %s%s\n' "$CYN" "$*" "$RST"; }
ok()   { printf '  %s✔%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '  %s!%s %s\n' "$YEL" "$RST" "$*"; }
die()  { printf '\n%s✗ 迁移中止 @ %s%s\n' "$RED" "$*" "$RST" >&2; exit 1; }
run()  { if (( DRY_RUN )); then printf '    %s[dry-run]%s %s\n' "$YEL" "$RST" "$*"; else eval "$*"; fi; }

main() {
  # 参数解析
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --cloudflared) CF_SRC="${2:-}"; shift 2 || die "参数解析:--cloudflared 缺路径" ;;
      --dry-run)     DRY_RUN=1; shift ;;
      -h|--help)     grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) die "参数解析:未知参数 '$1'(见 --help)" ;;
    esac
  done
  (( DRY_RUN )) && printf '%s*** DRY-RUN:只打印动作,不落盘 ***%s\n' "$YEL" "$RST"

  # ── ① 前置检查 ────────────────────────────────────────────────────────
  step "① 前置检查"
  [[ "${EUID}" -eq 0 ]]        || die "① 需 root(sudo)。"
  [[ -d "$OLD_DIR" ]]          || die "① 旧目录不存在:$OLD_DIR(可能已迁移?)"
  [[ ! -e "$NEW_DIR" ]]        || die "① 新目录已存在:$NEW_DIR —— 先移开或确认未半途迁过。"
  # 确认要迁移的检出里已经有改名后的单元文件(即本 feat/rename-nous-engine 已 merge/checkout),
  # 否则步骤⑥ cp 会找不到源 → 迁一半装不上单元。
  [[ -f "$OLD_DIR/infra/systemd/nous-engine-backend.service" ]] \
    || die "① $OLD_DIR 里没有改名后的单元(缺 nous-engine-backend.service)。先在该检出 checkout/merge feat/rename-nous-engine。"
  [[ -x "$OLD_DIR/infra/systemd/install.sh" ]] || die "① 缺 $OLD_DIR/infra/systemd/install.sh"
  id "$RUN_USER" >/dev/null 2>&1 || die "① 用户 $RUN_USER 不存在。"
  ok "旧目录在 · 新目录空 · 改名单元就位 · root · $RUN_USER 存在"
  [[ -n "$CF_SRC" ]] && { [[ -f "$CF_SRC" ]] || die "① --cloudflared 指向的文件不存在:$CF_SRC"; ok "cloudflared 源二进制:$CF_SRC"; }

  # ── ② 记录旧单元 enabled 状态(逐个 is-enabled,不硬编码)──────────────
  step "② 记录当前 enabled 状态(迁移后按原状恢复)"
  declare -A OLD_STATE=()
  local u st
  for u in "${OLD_UNIT_ORDER[@]}"; do
    st="$(systemctl is-enabled "$u" 2>/dev/null || echo not-found)"
    OLD_STATE[$u]="$st"
    printf '  %-26s %s\n' "$u" "$st"
  done

  # ── ③ 停运行中的旧单元 ─────────────────────────────────────────────────
  step "③ 停旧单元(backend 停会经 PartOf 带停 cloudflared)"
  # 停 target 不会连带停 Wants 的服务,故显式停各服务。not-found/未运行的 stop 无害。
  for u in nous.target nous-cloudflared.service nous-backend.service nous-status.service \
           nous-aligner.service nous-healthprobe.timer nous-dbbackup.timer; do
    if systemctl cat "$u" >/dev/null 2>&1; then run "systemctl stop '$u' 2>/dev/null || true"; fi
  done
  ok "旧单元已停(或本就未运行)"

  # ── ④ 保存 healthprobe drop-in + 删旧单元/nousctl/旧 sudoers ────────────
  step "④ 删旧 unit 文件 + 迁移 drop-in override"
  # 管理员手动 drop-in(如 defer-tunnel.conf: NOUS_TUNNEL_AUTOHEAL=0)不在仓库里,删前必须
  # 搬到新单元名下,否则会静默丢掉管理员的隧道自愈关闭覆盖。
  local old_dropin="$UNIT_DIR/nous-healthprobe.service.d"
  local new_dropin="$UNIT_DIR/nous-engine-healthprobe.service.d"
  if [[ -d "$old_dropin" ]]; then
    run "cp -a '$old_dropin' '$new_dropin'"
    ok "迁移 drop-in:$(basename "$old_dropin") → $(basename "$new_dropin")"
  fi
  # 删旧单元:仅任务约定的 6 组 + nous.target(gpu-guard/moss-asr 单独管理,不在此列)。
  # glob nous-<svc>* 不会误伤 nous-engine-*(前缀不同),也把上面已复制的旧 .d 一并清掉。
  local g
  for g in nous-backend nous-cloudflared nous-status nous-dbbackup nous-healthprobe nous-aligner; do
    run "rm -rf '$UNIT_DIR/${g}'* 2>/dev/null || true"
  done
  run "rm -f '$UNIT_DIR/nous.target' 2>/dev/null || true"
  run "rm -f /usr/local/bin/nousctl 2>/dev/null || true"
  run "rm -f /etc/sudoers.d/nous-deploy /etc/sudoers.d/nous-healthprobe 2>/dev/null || true"
  ok "旧 unit / nousctl / 旧 sudoers 已删"

  # ── ⑤ mv 仓库目录(自吃尾巴点:main() 已整体解析,安全)──────────────────
  step "⑤ mv 仓库目录 $OLD_DIR → $NEW_DIR"
  run "mv '$OLD_DIR' '$NEW_DIR'"
  [[ $DRY_RUN -eq 1 || -d "$NEW_DIR" ]] || die "⑤ mv 后新目录不存在,严重异常。"
  ok "目录已迁"

  # ── ⑥ 装新 unit + enginectl + sudoers + daemon-reload + 恢复 enable ────
  step "⑥ 安装新单元 / enginectl / sudoers,并按②恢复 enable"
  local SD="$NEW_DIR/infra/systemd" SEC="$NEW_DIR/infra/security"
  for u in "${NEW_UNIT_FILES[@]}"; do
    [[ $DRY_RUN -eq 1 || -f "$SD/$u" ]] || die "⑥ 缺新单元源文件 $SD/$u"
    run "install -m 0644 '$SD/$u' '$UNIT_DIR/$u'"
  done
  run "install -m 0755 '$SD/enginectl' '$ENGINECTL_DST'"
  local sd dst
  for sd in nous-engine-healthprobe nous-engine-deploy; do
    dst="/etc/sudoers.d/$sd"
    run "install -m 0440 '$SEC/$sd.sudoers' '$dst'"
    if (( ! DRY_RUN )); then visudo -cf "$dst" >/dev/null 2>&1 && ok "sudoers: $sd" || { rm -f "$dst"; die "⑥ sudoers $sd visudo 校验失败,已撤掉。"; }; fi
  done
  run "systemctl daemon-reload"
  ok "新单元 + enginectl + sudoers 已装,daemon-reload 完成"

  # 恢复 enable:仅对②记录为 enabled 的旧单元 enable --now 对应新单元;disabled/static/
  # not-found 保持不动(disabled 的 cloudflared 会由 nous-engine.target 的 Wants 拉起,与原状一致)。
  step "⑥b 按原 enabled 状态恢复"
  for u in "${OLD_UNIT_ORDER[@]}"; do
    local new="${OLD2NEW[$u]}"
    case "${OLD_STATE[$u]}" in
      enabled)
        run "systemctl enable --now '$new'" && ok "enable --now $new(原 $u=enabled)" ;;
      disabled)
        warn "$new 保持 disabled(原 $u=disabled;若在 target Wants 里会被总闸拉起)" ;;
      *)
        warn "$new 跳过(原 $u=${OLD_STATE[$u]})" ;;
    esac
  done

  # ── ⑦ cloudflared 二进制修复 ───────────────────────────────────────────
  step "⑦ cloudflared 二进制检查"
  if command -v cloudflared >/dev/null 2>&1 || [[ -x /usr/local/bin/cloudflared ]]; then
    ok "cloudflared 已在($(command -v cloudflared 2>/dev/null || echo /usr/local/bin/cloudflared))"
  elif [[ -n "$CF_SRC" ]]; then
    run "install -m 0755 '$CF_SRC' /usr/local/bin/cloudflared"
    ok "cloudflared 已从 $CF_SRC 安装到 /usr/local/bin/cloudflared(0755)"
  else
    warn "缺 /usr/local/bin/cloudflared 且未给 --cloudflared <path> → 跳过(公网隧道暂不可用,"
    warn "  不阻塞迁移;本机 + Tailscale 正常)。补:sudo install -m0755 <cloudflared> /usr/local/bin/"
  fi

  # ── ⑧ 迁移 Claude 记忆目录(以 heygo 身份)───────────────────────────────
  step "⑧ 迁移 Claude 记忆目录"
  local home; home="$(getent passwd "$RUN_USER" | cut -d: -f6)"
  local mem_old="$home/.claude/projects/-media-heygo-program-projects-code-repos-nous-center"
  local mem_new="$home/.claude/projects/-media-heygo-program-projects-code-repos-nous-engine"
  if [[ -d "$mem_old" ]]; then
    [[ -e "$mem_new" ]] && die "⑧ 记忆新目录已存在:$mem_new(手动核对后再迁)。"
    run "sudo -u '$RUN_USER' mv '$mem_old' '$mem_new'"
    ok "记忆目录已迁 → $(basename "$mem_new")"
  else
    warn "无 $mem_old,跳过(可能路径不同或未用过 Claude)"
  fi

  # ── ⑨ 健康检查 ─────────────────────────────────────────────────────────
  step "⑨ 健康检查"
  if (( DRY_RUN )); then
    warn "dry-run:跳过 healthz 轮询"
  else
    local code=000 i
    for i in $(seq 1 60); do
      code="$(curl -s --noproxy '*' -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/healthz 2>/dev/null || echo 000)"
      [[ "$code" == 200 ]] && break
      sleep 2
    done
    [[ "$code" == 200 ]] && ok "本机 /healthz → 200" \
      || warn "本机 /healthz → $code(120s 未 200;backend 可能仍在预热常驻模型,查 journalctl -u nous-engine-backend -n 80)"
  fi
  for u in "${NEW_UNIT_FILES[@]}"; do
    [[ "$u" == *.timer || "$u" == *.target || "$u" == *.service ]] || continue
    printf '  %-34s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || echo n/a)"
  done

  # ── ⑩ 完成摘要 + 回滚提示 ──────────────────────────────────────────────
  step "⑩ 完成"
  printf '%s✔ 迁移完成%s(nous-center → nous-engine)。后续手动收尾见 MIGRATION-nous-engine.md:\n' "$GRN" "$RST"
  echo  "   · GitHub 网页改仓库名 iocrazy/nous-center → nous-engine"
  echo  "   · git -C $NEW_DIR remote set-url origin https://github.com/iocrazy/nous-engine.git"
  echo  "   · 验证:enginectl status · 公网 https://api.iocrazy.com/healthz 200 · 引擎 loaded"
  echo  "   · (可选)前端/状态页品牌字样 nous-center → nous-engine(见 runbook 可选项)"
  printf '\n%s回滚%s(若健康检查未过且需还原):\n' "$YEL" "$RST"
  echo  "   1. sudo systemctl disable --now ${NEW_UNIT_FILES[*]} 2>/dev/null; sudo rm -f $UNIT_DIR/nous-engine-* $ENGINECTL_DST /etc/sudoers.d/nous-engine-*"
  echo  "   2. sudo mv $NEW_DIR $OLD_DIR   # 目录反向搬回"
  echo  "   3. cd $OLD_DIR && git checkout master -- infra/  # 从历史恢复旧 unit 文件"
  echo  "   4. sudo ./infra/systemd/install.sh   # 用旧(master)install.sh 重装 nous-* 单元"
  echo  "   5. (若迁过)sudo -u $RUN_USER mv <记忆新目录> <记忆旧目录>"
}

main "$@"
