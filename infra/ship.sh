#!/usr/bin/env bash
# nous-engine 「CI 全绿 → 合并 → 上线」一条命令(2026-09-09)。在**开发机**(Mac)上跑。
#
#   ./infra/ship.sh <PR号>                合并 + 上线
#   ./infra/ship.sh <PR号> --no-deploy    只合并,不上线
#   ./infra/ship.sh --deploy-only         不合并,只闸门 + 上线(闸门拒绝过一次之后的重试入口)
#
# 为什么是这个形状:仓库是 public,self-hosted runner 挂上去等于把生产机交给任何 fork;
# 轮询式又要在生产机上多养一个 timer。而合并这个动作本来就发生在 Mac 上,Mac 已能经
# ZeroTier ssh 到生产机 —— 扳机就是合并本身,合完直接 ssh 过去跑 deploy.sh。
# 零轮询、零新入口、零新凭据,生产机上什么都不用装。
#
# 接受的代价:不经本脚本的合并(手机点网页、dependabot auto-merge)不会自动上线,
# 生产停在上一版直到下一次 ship —— deploy.sh 是 reset 到 origin/master,会把中间的一并带上。
#
# 环境变量:NOUS_SHIP_HOST(ssh 目标,默认 ubuntu = ~/.ssh/config 里的 10.0.0.10)
#          NOUS_SHIP_PROD_DIR(生产检出,默认 /media/heygo/program/projects-code/repos/nous-engine)
#          NOUS_SHIP_DISCORD_WEBHOOK(可选;设了就把结果一行 POST 过去)
#
# 退出码(tests/test_ship_flow.py 锁住):
#   0 已合并且已上线      2 用法错 / PR 不可合(非 OPEN、draft、base 不是 master)
#   3 CI 红,未合并        4 合并失败
#   5 已合并但闸门拒绝上线(渲染进行中 / 开关关着)—— 稍后 --deploy-only
#   6 上线失败,或上线后生产 HEAD 不含本 PR 的 merge commit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${NOUS_SHIP_HOST:-ubuntu}"
PROD="${NOUS_SHIP_PROD_DIR:-/media/heygo/program/projects-code/repos/nous-engine}"
WEBHOOK="${NOUS_SHIP_DISCORD_WEBHOOK:-}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST")

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✅ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }
die()  { local rc="$1"; shift; printf '\033[1;31m❌ %s\033[0m\n' "$*" >&2; notify "❌ $*"; exit "$rc"; }
usage() { sed -n '3,6p' "${BASH_SOURCE[0]}" | sed 's/^# //' >&2; exit 2; }

notify() {
  [[ -n "$WEBHOOK" ]] || return 0
  local msg="nous-engine ship: $*"
  curl -s -m 10 -H 'Content-Type: application/json' \
    -d "$(printf '{"content": %s}' "$(printf '%s' "$msg" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")" \
    "$WEBHOOK" >/dev/null 2>&1 || warn "Discord webhook 发送失败(不影响结果)"
}

# ---------- 参数 ----------
PR=""; DEPLOY=1; MERGE=1
for a in "$@"; do
  case "$a" in
    --no-deploy)   DEPLOY=0 ;;
    --deploy-only) MERGE=0 ;;
    -h|--help)     usage ;;
    -*)            echo "未知参数 '$a'" >&2; usage ;;
    *)             [[ -z "$PR" ]] || usage; PR="$a" ;;
  esac
done
if (( MERGE )); then
  [[ "$PR" =~ ^[0-9]+$ ]] || usage
else
  [[ -z "$PR" ]] || { echo "--deploy-only 不接 PR 号" >&2; usage; }
  (( DEPLOY )) || { echo "--deploy-only 与 --no-deploy 互斥" >&2; usage; }
fi

sha=""
if (( MERGE )); then
  # ---------- 1. PR 可合? ----------
  step "PR #$PR 状态"
  gh auth status >/dev/null 2>&1 || die 2 "gh 未登录(gh auth login)。"
  meta="$(gh pr view "$PR" --json state,isDraft,baseRefName,title)" || die 2 "读不到 PR #$PR。"
  read -r state draft base title < <(printf '%s' "$meta" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d["state"], str(d["isDraft"]).lower(), d["baseRefName"], d.get("title", "").replace(" ", "_")[:60])')
  [[ "$state" == "OPEN" ]]  || die 2 "PR #$PR 不是 OPEN(是 $state),没什么可合的。"
  [[ "$draft" == "false" ]] || die 2 "PR #$PR 还是 draft。"
  [[ "$base" == "master" ]] || die 2 "PR #$PR 的 base 是 $base,不是 master —— ship 只上 master。"
  ok "OPEN,对着 master:${title//_/ }"

  # ---------- 2. CI 全绿(pending 就等) ----------
  step "等 CI(gh pr checks --watch)"
  gh pr checks "$PR" --watch --fail-fast || die 3 "PR #$PR CI 红,不合。"
  ok "CI 全绿"

  # ---------- 3. 合并 ----------
  step "squash 合并 + 删分支"
  gh pr merge "$PR" --squash --delete-branch || die 4 "合并失败(冲突?分支保护?)。"
  sha="$(gh pr view "$PR" --json mergeCommit -q .mergeCommit.oid 2>/dev/null || true)"
  ok "已合并 → master ${sha:0:12}"
  if (( ! DEPLOY )); then
    notify "✅ #$PR 已合并(${sha:0:12}),按 --no-deploy 未上线"
    printf '\n\033[1;32m✅ 已合并,未上线(--no-deploy)。上线:./infra/ship.sh --deploy-only\033[0m\n'
    exit 0
  fi
fi

# ---------- 4. 闸门(灌仓库里这份 guard 到生产机执行) ----------
step "上线前闸门 @ $HOST:$PROD"
guard_out="$("${SSH[@]}" "cd '$PROD' && bash -s" < "$SCRIPT_DIR/autodeploy-guard.sh" 2>&1)" && guard_rc=0 || guard_rc=$?
printf '%s\n' "$guard_out"
if (( guard_rc != 0 )); then
  if (( MERGE )); then
    notify "⏸ #$PR 已合并(${sha:0:12}),未上线 — $guard_out"
    printf '\n\033[1;33m⏸ 已合并,未上线:闸门拒绝(见上)。稍后 ./infra/ship.sh --deploy-only\033[0m\n' >&2
  else
    notify "⏸ 未上线 — $guard_out"
    printf '\n\033[1;33m⏸ 未上线:闸门拒绝(见上)。\033[0m\n' >&2
  fi
  exit 5
fi

# ---------- 5. 上线(deploy.sh 自带全部校验:同步/venv/build/重启/新实例确认) ----------
step "上线(deploy.sh @ $HOST)"
"${SSH[@]}" "cd '$PROD' && ./infra/deploy.sh" || die 6 "deploy.sh 失败(输出见上;journalctl -u nous-engine-backend -n 50)。"

# ---------- 6. 校验:生产 HEAD 真含本次 merge commit ----------
if [[ -n "$sha" ]]; then
  step "校验生产 HEAD 含 ${sha:0:12}"
  "${SSH[@]}" "git -C '$PROD' merge-base --is-ancestor $sha HEAD" \
    || die 6 "deploy.sh 报成功,但生产 HEAD 不含 $sha —— 上的不是这个 PR,查 git -C $PROD log。"
  ok "含 ${sha:0:12}"
fi

if (( MERGE )); then
  notify "🚀 #$PR 已合并并上线(${sha:0:12})"
  printf '\n\033[1;32m🚀 #%s 已合并并上线 — %s\033[0m\n' "$PR" "$sha"
else
  notify "🚀 已上线(--deploy-only)"
  printf '\n\033[1;32m🚀 已上线(--deploy-only)\033[0m\n'
fi
