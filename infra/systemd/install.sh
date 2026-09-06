#!/usr/bin/env bash
# 把 nous-engine 全栈装成 systemd 服务(开机自启 + 崩溃自重启 + journald),取代 nohup。
# 幂等 —— 可反复跑。
#
# Usage:
#   sudo ./infra/systemd/install.sh            # install + enable + start + 自检
#   sudo ./infra/systemd/install.sh uninstall  # stop + disable + remove
#
# 装完自检 + 访问地址会打印在下面 banner 里。日志:
#   journalctl -u nous-engine-backend -f    ·    enginectl status    ·    enginectl logs [backend|status|...]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET=/etc/systemd/system
ENGINECTL_DST=/usr/local/bin/enginectl

# 长驻服务。公网隧道 nous-engine-cloudflared 已于 2026-09-06 退役(用户决定:nous-engine
# 只在本机/局域网/ZeroTier 可达,不再对外暴露);下面第 2 步会主动清掉旧单元并 mask。
SERVICES=(nous-engine-backend.service nous-engine-status.service)
TIMERS=(nous-engine-healthprobe.timer nous-engine-dbbackup.timer)
TARGETS=(nous-engine.target)
SUDOERS=(nous-engine-deploy)
# 全部拷进 /etc/systemd/system(含 oneshot probe/dbbackup、target、comfyui)。
UNIT_FILES=(nous-engine-backend.service nous-engine-status.service \
            nous-engine-healthprobe.service nous-engine-healthprobe.timer nous-engine-dbbackup.service nous-engine-dbbackup.timer nous-engine-comfyui.service nous-engine-netprobe.service nous-engine.target)

LOCAL_URL="${NOUS_LOCAL_URL:-http://127.0.0.1:8000}"
ZT_URL="${NOUS_ZT_URL:-http://10.0.0.10:8000}"
STATUS_URL="${NOUS_STATUS_URL:-http://127.0.0.1:8001}"

# ── 样式 ──────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[1;32m'; RED=$'\033[1;31m'; YEL=$'\033[1;33m'; CYN=$'\033[1;36m'; RST=$'\033[0m'
else B=""; DIM=""; GRN=""; RED=""; YEL=""; CYN=""; RST=""; fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s▸ %s%s\n' "$CYN" "$*" "$RST"; }
ok()   { printf '  %s✔%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '  %s!%s %s\n' "$YEL" "$RST" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$RED" "$RST" "$*"; }
rule() { printf '%s────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"; }

if [[ "${EUID}" -ne 0 ]]; then echo "ERROR: 需 root(sudo)" >&2; exit 1; fi

# 探一个 HTTP 端点,回显 code(不因 set -e 中止)。
probe() { curl -s --noproxy '*' -m "${2:-5}" -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || echo 000; }
svc_active() { systemctl is-active --quiet "$1" 2>/dev/null; }

case "${1:-install}" in
  install)
    printf '\n%s╔══════════════════════════════════════════════════════════╗%s\n' "$B" "$RST"
    printf   '%s║   nous-engine · systemd 全栈安装                          ║%s\n' "$B" "$RST"
    printf   '%s╚══════════════════════════════════════════════════════════╝%s\n' "$B" "$RST"
    say "${DIM}检出: $(cd "$SCRIPT_DIR/../.." && pwd)${RST}"

    # ── 1. 单元文件 ──────────────────────────────────────────────────────
    step "安装 systemd 单元 → $TARGET"
    for u in "${UNIT_FILES[@]}"; do install -m 0644 "$SCRIPT_DIR/$u" "$TARGET/$u"; done
    systemctl daemon-reload
    ok "${#UNIT_FILES[@]} 个单元已安装 + daemon-reload"

    # ── 2. 启用 + 启动长驻服务 ───────────────────────────────────────────
    step "启用 + 启动服务(开机自启)"
    for svc in "${SERVICES[@]}"; do
      if systemctl enable --now "$svc" >/dev/null 2>&1; then ok "$svc"
      else bad "$svc 启动失败 — 查 journalctl -u $svc -n 50"; fi
    done

    # netprobe(spec 2026-09-06 process-net-traffic):root bpftrace 采集每进程网络流量。
    # 机器上有 bpftrace 才装;先 --dry-run 附着一次全部探针,符号缺失就明说、不启。
    if command -v bpftrace >/dev/null 2>&1; then
      if bpftrace --dry-run "$SCRIPT_DIR/../monitoring/netprobe.bt" >/tmp/nous-netprobe-dryrun.log 2>&1; then
        if systemctl enable --now nous-engine-netprobe.service >/dev/null 2>&1; then ok "nous-engine-netprobe(每进程网络流量)"
        else bad "nous-engine-netprobe 启动失败 — 查 journalctl -u nous-engine-netprobe -n 50"; fi
      else
        systemctl disable --now nous-engine-netprobe.service >/dev/null 2>&1 || true
        bad "netprobe 探针附着失败(内核符号缺?)— 见 /tmp/nous-netprobe-dryrun.log;面板「网络」列将显示 —"
      fi
    else
      systemctl disable --now nous-engine-netprobe.service >/dev/null 2>&1 || true
      warn "未装 bpftrace → 跳过 nous-engine-netprobe(面板「网络」列显示 —);apt install bpftrace 后重跑本脚本"
    fi

    # 隧道退役后 healthprobe 的 NOUS_TUNNEL_AUTOHEAL drop-in 已无意义,顺手清掉。
    if [[ -d "$TARGET/nous-engine-healthprobe.service.d" ]]; then
      rm -rf "$TARGET/nous-engine-healthprobe.service.d"; systemctl daemon-reload
      ok "已清除 nous-engine-healthprobe.service.d(隧道自愈 drop-in,已失效)"
    fi

    # 公网隧道已退役(2026-09-06):把历史遗留的 nous-engine-cloudflared 清干净并 mask,
    # 保证重装/迁移/任何 PartOf 联动都不会再把它拉起来;其免密重启 sudoers 一并删除。
    systemctl disable --now nous-engine-cloudflared.service >/dev/null 2>&1 || true
    rm -f "$TARGET/nous-engine-cloudflared.service" /etc/sudoers.d/nous-engine-healthprobe
    systemctl daemon-reload
    systemctl mask nous-engine-cloudflared.service >/dev/null 2>&1 || true
    ok "nous-engine-cloudflared 已退役(单元移除 + mask,不再对外暴露)"

    # ComfyUI sidecar:不默认启用 —— 安装前须核对(见下面说明)。
    systemctl disable nous-engine-comfyui.service >/dev/null 2>&1 || true
    warn "nous-engine-comfyui 单元已安装但未启用(安装前置条件检查)"
    warn "  前置:ComfyUI 安装在 WorkingDirectory + venv + CUDA_VISIBLE_DEVICES 验证"
    warn "  ① 核对 GPU: nvidia-smi --query-gpu=index,name --format=csv(PCI_BUS_ID 排序)"
    warn "  ② 编辑 $(realpath "$SCRIPT_DIR/nous-engine-comfyui.service") → 设 WorkingDirectory + CUDA_VISIBLE_DEVICES"
    warn "  ③ sudo systemctl daemon-reload && sudo systemctl enable --now nous-engine-comfyui"

    # ── 3. 定时器 + 总闸 ────────────────────────────────────────────────
    step "启用定时器 + 总闸"
    for tmr in "${TIMERS[@]}"; do systemctl enable --now "$tmr" >/dev/null 2>&1 && ok "$tmr"; done
    for tgt in "${TARGETS[@]}"; do systemctl enable "$tgt" >/dev/null 2>&1 && ok "$tgt (开机总闸)"; done

    # ── 4. enginectl + sudoers ────────────────────────────────────────────
    step "安装 enginectl + sudoers drop-ins"
    install -m 0755 "$SCRIPT_DIR/enginectl" "$ENGINECTL_DST"; ok "enginectl → $ENGINECTL_DST"
    for sd in "${SUDOERS[@]}"; do
      src="$SCRIPT_DIR/../security/$sd.sudoers"; dst="/etc/sudoers.d/$sd"
      install -m 0440 "$src" "$dst"
      if visudo -cf "$dst" >/dev/null 2>&1; then ok "sudoers: $sd"; else bad "sudoers $sd 校验失败,已撤掉"; rm -f "$dst"; fi
    done

    # ── 5. 自检(等后端就绪最多 ~30s)────────────────────────────────────
    step "自检"
    code=000
    for _ in $(seq 1 15); do code="$(probe "$LOCAL_URL/healthz")"; [[ "$code" == 200 ]] && break; sleep 2; done

    for svc in postgresql "${SERVICES[@]}" nous-engine-netprobe.service; do
      if svc_active "$svc"; then ok "$(printf '%-24s active' "$svc")"; else bad "$(printf '%-24s %s' "$svc" "$(systemctl is-active "$svc" 2>/dev/null || echo inactive)")"; fi
    done

    if [[ "$code" == 200 ]]; then
      ok "本机 /healthz  → 200"
      # 组件详情(database / gpus / 常驻模型)
      health="$(curl -s --noproxy '*' -m 5 "$LOCAL_URL/health" 2>/dev/null || echo '{}')"
      db=$(printf '%s' "$health"  | grep -o '"database":"[^"]*"' | cut -d'"' -f4)
      gpus=$(printf '%s' "$health"| grep -o '"gpus":[0-9]*'      | cut -d: -f2)
      [[ -n "$db"   ]] && { [[ "$db" == ok ]] && ok "database    → ok" || bad "database    → $db"; }
      [[ -n "$gpus" ]] && ok "GPU 识别    → $gpus 张"
    else
      bad "本机 /healthz  → $code(后端可能还在预加载常驻模型,稍等再 enginectl status)"
    fi

    zt="$(probe "$ZT_URL/healthz")";     [[ "$zt" == 200 ]] && ok "ZeroTier /healthz → 200" || warn "ZeroTier /healthz → $zt(10.0.0.10 未分配?)"

    # ── 访问地址 + 收尾 ─────────────────────────────────────────────────
    printf '\n%s╭─ 访问地址 ────────────────────────────────────────────────%s\n' "$B" "$RST"
    printf   '%s│%s  本机管理台   %s%s%s\n'   "$B" "$RST" "$CYN" "$LOCAL_URL"  "$RST"
    printf   '%s│%s  ZeroTier 内网 %s%s%s\n'  "$B" "$RST" "$CYN" "$ZT_URL"     "$RST"
    printf   '%s│%s  独立状态页   %s%s%s\n'   "$B" "$RST" "$CYN" "$STATUS_URL" "$RST"
    printf   '%s╰──────────────────────────────────────────────────────────%s\n' "$B" "$RST"

    printf '\n%s✔ 安装完成%s — 服务已开机自启 + 崩溃自重启。\n' "$GRN" "$RST"
    say "  管控:   ${B}enginectl${RST} status | up | down | restart | logs"
    say "  日志:   ${B}journalctl -u nous-engine-backend -f${RST}"
    say "  健康巡检: 每 2 分钟(journalctl -u nous-engine-healthprobe -f)"
    ;;

  uninstall)
    printf '\n%s▸ 卸载 nous-engine systemd 栈%s\n' "$CYN" "$RST"
    for tgt in "${TARGETS[@]}"; do systemctl disable "$tgt" 2>/dev/null || true; rm -f "$TARGET/$tgt"; ok "移除 $tgt"; done
    for tmr in "${TIMERS[@]}"; do systemctl disable --now "$tmr" 2>/dev/null || true; rm -f "$TARGET/$tmr"; ok "移除 $tmr"; done
    for svc in "${SERVICES[@]}"; do systemctl disable --now "$svc" 2>/dev/null || true; rm -f "$TARGET/$svc"; ok "移除 $svc"; done
    systemctl disable --now nous-engine-netprobe.service 2>/dev/null || true; rm -f "$TARGET/nous-engine-netprobe.service"; ok "移除 nous-engine-netprobe.service"
    rm -f "$TARGET/nous-engine-healthprobe.service" "$TARGET/nous-engine-dbbackup.service"
    rm -f "$ENGINECTL_DST"
    for sd in "${SUDOERS[@]}"; do rm -f "/etc/sudoers.d/$sd"; done
    systemctl daemon-reload
    printf '%s✔ 已卸载%s(postgresql 保留)。\n' "$GRN" "$RST"
    ;;

  *)
    echo "Usage: $0 [install|uninstall]" >&2; exit 1 ;;
esac
