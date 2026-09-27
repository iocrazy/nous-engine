#!/usr/bin/env bash
# 给**已存在**的 self-hosted runner 装同一道 job-started 硬闸(2026-09-27)。
# 主要给同机的 nous-app runner 用:iocrazy/nous-app 现在是 public,fork PR 审批只到
# first_time_contributors,runner 上没有任何本地拦截。
#
#   ./infra/runner/harden-runner.sh <runner 目录> "<允许的 GITHUB_WORKFLOW_REF,空格分隔>" "<允许的事件>"
#
# nous-app 示例(三条留在本机的链:deploy-gpu / run-migration / config-drift):
#   ./infra/runner/harden-runner.sh /media/heygo/program/datahub/nous/data/runner \
#     "iocrazy/nous-app/.github/workflows/deploy-gpu.yml@refs/heads/master iocrazy/nous-app/.github/workflows/run-migration.yml@refs/heads/master iocrazy/nous-app/.github/workflows/config-drift.yml@refs/heads/master" \
#     "push schedule workflow_dispatch"
set -euo pipefail
[[ $# -eq 3 ]] || { sed -n 2,12p "$0" >&2; exit 2; }
RUNNER_DIR="$1"; ALLOWED_WORKFLOWS="$2"; ALLOWED_EVENTS="$3"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$RUNNER_DIR/.runner" ]] || { echo "❌ $RUNNER_DIR 不是已注册的 runner 目录" >&2; exit 1; }

install -d "$RUNNER_DIR/hooks"
install -m 0755 "$SCRIPT_DIR/job-started-guard.sh" "$RUNNER_DIR/hooks/job-started-guard.sh"
envf="$RUNNER_DIR/.env"; touch "$envf"; tmp="$(mktemp)"
grep -v -E '^(ACTIONS_RUNNER_HOOK_JOB_STARTED|RUNNER_GUARD_ALLOWED_WORKFLOWS|RUNNER_GUARD_ALLOWED_EVENTS)=' "$envf" > "$tmp" || true
{
  cat "$tmp"
  echo "ACTIONS_RUNNER_HOOK_JOB_STARTED=$RUNNER_DIR/hooks/job-started-guard.sh"
  echo "RUNNER_GUARD_ALLOWED_WORKFLOWS=$ALLOWED_WORKFLOWS"
  echo "RUNNER_GUARD_ALLOWED_EVENTS=$ALLOWED_EVENTS"
} > "$envf"
rm -f "$tmp"
svc="$(cat "$RUNNER_DIR/.service")"
sudo systemctl restart "$svc"
sleep 2
systemctl is-active --quiet "$svc" && echo "✅ $svc 已装硬闸并重启" || { echo "❌ $svc 未 active" >&2; exit 1; }
