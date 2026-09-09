#!/usr/bin/env bash
# nous-engine 上线前闸门(2026-09-09)。在**生产机**上、生产检出根目录里执行;
# 通常不是手动跑,而是 Mac 上的 infra/ship.sh 经 `ssh host 'cd <prod> && bash -s' < 本文件`
# 灌过来 —— 所以执行的永远是**正在被上线的那一版**的闸门,不依赖生产机上已有的拷贝。
#
# 退出码:0 放行 / 5 拒绝(stdout 说明原因)。任何一条拒绝都只是「这次先不上」,
# 合并已经完成,稍后 `ship.sh --deploy-only` 重试,或人自己 ./infra/deploy.sh(人可以越过闸门)。
#
# 三道闸,顺序即优先级:
#   1. 必须在生产检出(.nous-production 标记,与 deploy.sh 同一条防呆)
#   2. .nous-autodeploy-off 存在 → 关着(enginectl autodeploy on|off 开关)
#   3. ComfyUI 有渲染在跑 → 不砍。deploy.sh 重启后端会卸掉全部模型;一个渲染能跑 4 小时,
#      拦腰砍掉等于白烧几小时的卡。探的是 /api/v1/comfy/health 的 running_render
#      (谁占着渲染信号量、占了多久)。后端不可达 / 响应不是 JSON → 没有渲染可保护,
#      **放行**:那种状态下上线是修复,不是破坏。
set -euo pipefail

REPO="${NOUS_PROD_DIR:-$PWD}"
LOCAL="${NOUS_LOCAL_URL:-http://127.0.0.1:8000}"
OFF_MARKER="$REPO/.nous-autodeploy-off"

refuse() { printf 'guard: ✋ %s\n' "$*"; exit 5; }
allow()  { printf 'guard: ✅ %s\n' "$*"; exit 0; }

# ── 1. 生产检出 ──────────────────────────────────────────────────────────────
[[ -f "$REPO/.nous-production" ]] || refuse "$REPO 不是生产检出(缺 .nous-production 标记),拒绝。"

# ── 2. 开关 ──────────────────────────────────────────────────────────────────
if [[ -e "$OFF_MARKER" ]]; then
  since="$(date -r "$OFF_MARKER" '+%m-%d %H:%M' 2>/dev/null || echo '?')"
  refuse "自动上线已关闭(.nous-autodeploy-off,自 $since)。\`enginectl autodeploy on\` 恢复。"
fi

# ── 3. 渲染进行中 ────────────────────────────────────────────────────────────
# ADMIN_TOKEN 从生产的 backend/.env 取(/api/v1/* 要 admin 鉴权;bearer 形式即可)。
token="$(grep -E '^ADMIN_TOKEN=' "$REPO/backend/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'" || true)"
# --noproxy '*':本机跑着 mihomo,不绕开会把回环地址探到代理上去。
resp="$(curl -s -m 5 --noproxy '*' -H "Authorization: Bearer $token" "$LOCAL/api/v1/comfy/health" 2>/dev/null)" || resp=""
[[ -n "$resp" ]] || allow "后端不可达($LOCAL),没有渲染可保护,放行(上线即修复)。"

running="$(printf '%s' "$resp" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(3)
r = d.get("running_render") if isinstance(d, dict) else None
if r:
    print("%s 已占 %ss" % (r.get("task_id"), r.get("held_seconds")))
' 2>&1)" || { allow "comfy/health 响应不是 JSON,视为空闲,放行。"; }

[[ -z "$running" ]] || refuse "ComfyUI 渲染进行中($running),不砍。渲染完后 \`ship.sh --deploy-only\`。"
allow "空闲,可上线。"
