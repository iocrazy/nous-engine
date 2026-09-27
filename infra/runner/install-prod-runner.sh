#!/usr/bin/env bash
# 在生产机上装 nous-engine 的 self-hosted 部署 runner(2026-09-27)。以 heygo 身份跑,需要时会
# 自己 sudo(装 systemd 服务、写 sudoers)。可重复执行:已注册则只刷新 hook / sudoers。
#
#   ./infra/runner/install-prod-runner.sh
#
# 做五件事:
#   1. 在 RUNNER_DIR 解出 runner(复用 nous-app runner 目录里的 runner.tgz,同版本、免下载)
#   2. 注册到 iocrazy/nous-engine:--no-default-labels,只有专属 label nous-engine-prod
#   3. 装 job-started 硬闸 hook(infra/runner/job-started-guard.sh 拷到仓库**之外**)+ 白名单
#   4. sudoers:只放行 deploy.sh 用到的那一条 `systemctl --no-block restart nous-engine-backend`
#      (visudo -c 校验通过才落盘)
#   5. 仓库 fork PR 审批改为 all_external_contributors(软闸,硬闸是第 3 步)
set -euo pipefail

REPO_SLUG="${REPO_SLUG:-iocrazy/nous-engine}"
RUNNER_DIR="${RUNNER_DIR:-/media/heygo/program/datahub/nous/data/runner-engine}"
RUNNER_NAME="${RUNNER_NAME:-heygo-ubuntu-engine}"
RUNNER_LABEL="${RUNNER_LABEL:-nous-engine-prod}"
TGZ_SOURCE="${TGZ_SOURCE:-/media/heygo/program/datahub/nous/data/runner/runner.tgz}"
ALLOWED_WORKFLOWS="${REPO_SLUG}/.github/workflows/deploy.yml@refs/heads/master"
ALLOWED_EVENTS="workflow_run workflow_dispatch"
SUDOERS_FILE=/etc/sudoers.d/nous-engine-deploy
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✅ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m❌ %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(id -un)" == "heygo" ]] || die "请以 heygo 身份运行(runner 与 deploy.sh 都以 heygo 跑),不要 sudo 整个脚本。"
command -v gh >/dev/null || die "缺 gh CLI。"
gh auth status >/dev/null 2>&1 || die "gh 未登录(gh auth login)。"

# ---------- 1. 解出 runner ----------
step "runner 目录 $RUNNER_DIR"
mkdir -p "$RUNNER_DIR"
if [[ ! -x "$RUNNER_DIR/config.sh" ]]; then
  [[ -f "$TGZ_SOURCE" ]] || die "找不到 $TGZ_SOURCE;设 TGZ_SOURCE 指向 actions-runner-linux-x64-*.tar.gz。"
  tar xzf "$TGZ_SOURCE" -C "$RUNNER_DIR"
  ok "已解出 runner"
else
  ok "runner 已存在,跳过解包"
fi

# ---------- 2. 注册 ----------
step "注册到 $REPO_SLUG(label: $RUNNER_LABEL)"
if [[ -f "$RUNNER_DIR/.runner" ]]; then
  ok "已注册($(python3 -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8-sig"))["agentName"])' "$RUNNER_DIR/.runner")),跳过"
else
  token="$(gh api -X POST "repos/$REPO_SLUG/actions/runners/registration-token" -q .token)"
  (cd "$RUNNER_DIR" && ./config.sh --unattended --url "https://github.com/$REPO_SLUG" --token "$token" \
      --name "$RUNNER_NAME" --no-default-labels --labels "$RUNNER_LABEL" --work _work --replace)
  ok "已注册"
fi

# ---------- 3. 硬闸 hook ----------
step "job-started 硬闸"
install -d "$RUNNER_DIR/hooks"
install -m 0755 "$SCRIPT_DIR/job-started-guard.sh" "$RUNNER_DIR/hooks/job-started-guard.sh"
envf="$RUNNER_DIR/.env"
touch "$envf"
tmp="$(mktemp)"
grep -v -E '^(ACTIONS_RUNNER_HOOK_JOB_STARTED|RUNNER_GUARD_ALLOWED_WORKFLOWS|RUNNER_GUARD_ALLOWED_EVENTS)=' "$envf" > "$tmp" || true
{
  cat "$tmp"
  echo "ACTIONS_RUNNER_HOOK_JOB_STARTED=$RUNNER_DIR/hooks/job-started-guard.sh"
  echo "RUNNER_GUARD_ALLOWED_WORKFLOWS=$ALLOWED_WORKFLOWS"
  echo "RUNNER_GUARD_ALLOWED_EVENTS=$ALLOWED_EVENTS"
} > "$envf"
rm -f "$tmp"
ok "hook 已装:$RUNNER_DIR/hooks/job-started-guard.sh(白名单 $ALLOWED_WORKFLOWS)"

# ---------- 4. sudoers ----------
step "sudoers:仅放行 systemctl --no-block restart nous-engine-backend"
rule='heygo ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block restart nous-engine-backend'
stage="$(mktemp)"
printf '# nous-engine 自动上线(infra/runner/install-prod-runner.sh):deploy.sh 只需要这一条\n%s\n' "$rule" > "$stage"
sudo visudo -cf "$stage" >/dev/null || { rm -f "$stage"; die "sudoers 片段校验失败,未写入。"; }
sudo install -m 0440 -o root -g root "$stage" "$SUDOERS_FILE"
rm -f "$stage"
sudo visudo -c >/dev/null || die "整体 sudoers 校验失败 —— 立刻检查 $SUDOERS_FILE!"
ok "已写 $SUDOERS_FILE"

# ---------- 服务 ----------
step "systemd 服务"
svc="$(cd "$RUNNER_DIR" && cat .service 2>/dev/null || true)"
if [[ -z "$svc" ]]; then
  (cd "$RUNNER_DIR" && sudo ./svc.sh install heygo)
  svc="$(cat "$RUNNER_DIR/.service")"
fi
sudo systemctl restart "$svc"   # 重启让 .env 里的 hook 配置生效
sleep 2
systemctl is-active --quiet "$svc" || die "$svc 未 active:journalctl -u $svc -n 50"
ok "$svc active"

# ---------- 5. fork PR 审批 ----------
step "fork PR 审批 → all_external_contributors"
gh api -X PUT "repos/$REPO_SLUG/actions/permissions/fork-pr-contributor-approval" \
  -f approval_policy=all_external_contributors >/dev/null
ok "$(gh api "repos/$REPO_SLUG/actions/permissions/fork-pr-contributor-approval" -q .approval_policy)"

printf '\n\033[1;32m🚀 完成。验证:gh workflow run deploy.yml -R %s\033[0m\n' "$REPO_SLUG"
