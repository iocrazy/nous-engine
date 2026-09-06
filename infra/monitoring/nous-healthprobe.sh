#!/usr/bin/env bash
# nous-engine 本地健康巡检(2026-06-16 稳定性加固,本地巡检+日志阶段)。
#
# 由 nous-engine-healthprobe.timer 每 2 分钟触发一次,探 vLLM 看门狗管不到的事:
#   1. 后端本机存活      (GET 127.0.0.1:8000/healthz → 200)
#   2. 后端自报健康      (GET /health → status/database/load_failures)
#   3. 公网隧道存活      (GET <public>/health → 非 530/000)—— 默认关闭,见下
#
# 输出结构化行到 stdout(systemd timer → journald;`journalctl -u nous-engine-healthprobe`)。
# 退出码:有「硬故障」(后端连不上 / DB 挂)→ 非 0(systemd 标 failed,
# 将来挂 OnFailure= 告警 hook 即可直接接告警通道);仅「软降级」(degraded /
# load_failures)→ 0 但日志 WARN。
#
# 公网隧道探针与自愈已于 2026-09-06 随 nous-engine-cloudflared 退役一并移除(用户决定:
# nous-engine 不再对外暴露,只在本机/局域网/ZeroTier 可达)。
#
# 手动单跑:infra/monitoring/nous-healthprobe.sh
set -uo pipefail

LOCAL_BASE="${NOUS_LOCAL_URL:-http://127.0.0.1:8000}"
TIMEOUT="${NOUS_PROBE_TIMEOUT:-10}"
STATE_FILE="${XDG_RUNTIME_DIR:-/tmp}/nous-healthprobe.state"

CURL=(curl -s --noproxy '*' --max-time "$TIMEOUT")
alerts=()   # 硬故障
warns=()    # 软降级

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

# --- 1. 后端本机存活 ---
live_code="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "$LOCAL_BASE/healthz" 2>/dev/null || echo 000)"
if [[ "$live_code" != "200" ]]; then
  alerts+=("backend-local-down(/healthz=$live_code)")
fi

# --- 2. 后端自报健康(只在本机活着时才解析)---
if [[ "$live_code" == "200" ]]; then
  health_json="$("${CURL[@]}" "$LOCAL_BASE/health" 2>/dev/null || echo '')"
  parsed="$(printf '%s' "$health_json" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_FAIL"); sys.exit(0)
db = d.get("database")
lf = d.get("load_failures") or {}
out = []
# 只报「可执行」信号。不报裸 status==degraded:本部署 Lane-K llm runner supervisor
# 常驻 running:false → status 恒 degraded,但 vLLM 由 model_manager 独立 spawn、LLM
# 服务照常(qwen35 真机验过)。报它=每 2 分钟一次噪声。DB 挂(硬)/ 模型加载失败(软)
# 才是真要看的;后端死由本脚本第 1 项探针覆盖。
if db != "ok":
    out.append("ALERT db=%s" % db)
if lf:
    out.append("WARN load_failures=%s" % ",".join(lf.keys()))
print("\n".join(out))
' 2>/dev/null || echo 'PARSE_FAIL')"
  if [[ "$parsed" == "PARSE_FAIL" ]]; then
    warns+=("health-json-unparseable")
  else
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      case "$line" in
        ALERT\ *) alerts+=("${line#ALERT }") ;;
        WARN\ *)  warns+=("${line#WARN }") ;;
      esac
    done <<< "$parsed"
  fi
fi

# --- 连续计数(状态文件:<硬故障streak> <ts>;旧格式第二列是隧道 streak,读时忽略)---
prev_hard=0
# shellcheck disable=SC2034  # _rest 仅占位
[[ -f "$STATE_FILE" ]] && read -r prev_hard _rest < "$STATE_FILE" 2>/dev/null || true
[[ "$prev_hard" =~ ^[0-9]+$ ]] || prev_hard=0

cur_hard=0
(( ${#alerts[@]} > 0 )) && cur_hard=$((prev_hard + 1))

echo "$cur_hard $(ts)" > "$STATE_FILE" 2>/dev/null || true

# --- 日志 + 退出码 ---
if (( ${#alerts[@]} > 0 )); then
  echo "[ALERT] $(ts) 连续第 ${cur_hard} 次 — $(IFS='; '; echo "${alerts[*]}")${warns:+; 降级: $(IFS='; '; echo "${warns[*]}")}"
  exit 1
fi
if (( ${#warns[@]} > 0 )); then
  echo "[WARN]  $(ts) 后端活但有降级 — $(IFS='; '; echo "${warns[*]}")"
  exit 0
fi
echo "[OK]    $(ts) backend healthy (local=$live_code)"
exit 0
