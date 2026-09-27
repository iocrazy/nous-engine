#!/usr/bin/env bash
# self-hosted 生产 runner 的硬闸(2026-09-27)。runner 在**每个 job 开始前**执行本脚本
# (runner 目录 .env 里 ACTIONS_RUNNER_HOOK_JOB_STARTED=<安装后的路径>),非 0 退出 = job 失败,
# 一行 workflow 代码都不会跑。
#
# 为什么需要:public 仓库挂 self-hosted runner,真正的风险不是我们自己的 deploy.yml(它不吃
# pull_request),而是 fork PR **自带**一个 `on: pull_request` + `runs-on: [self-hosted, …]` 的
# workflow —— GitHub 的 fork 审批策略只是软闸(点错一次就放行)。本脚本装在仓库**之外**
# (infra/runner/install-prod-runner.sh 拷过去),PR 改不到它:
#   只放行「白名单里的 workflow 文件 @ master」+「白名单事件」,其余一律拒。
#
# 配置(runner .env,由安装脚本写入):
#   RUNNER_GUARD_ALLOWED_WORKFLOWS  空格分隔的 GITHUB_WORKFLOW_REF 全串,
#                                   如 iocrazy/nous-engine/.github/workflows/deploy.yml@refs/heads/master
#   RUNNER_GUARD_ALLOWED_EVENTS     空格分隔的事件名,如 "workflow_run workflow_dispatch"
# 未配置 → 拒绝(fail-closed)。
set -uo pipefail

deny() { printf 'runner guard: ✋ %s\n' "$*" >&2; printf '::error::runner guard: %s\n' "$*"; exit 1; }

allowed_workflows="${RUNNER_GUARD_ALLOWED_WORKFLOWS:-}"
allowed_events="${RUNNER_GUARD_ALLOWED_EVENTS:-}"
workflow_ref="${GITHUB_WORKFLOW_REF:-}"
event="${GITHUB_EVENT_NAME:-}"
ref="${GITHUB_REF:-}"

[[ -n "$allowed_workflows" && -n "$allowed_events" ]] \
  || deny "未配置白名单(RUNNER_GUARD_ALLOWED_WORKFLOWS / RUNNER_GUARD_ALLOWED_EVENTS),拒绝一切。"
[[ -n "$workflow_ref" && -n "$event" ]] || deny "缺 GITHUB_WORKFLOW_REF / GITHUB_EVENT_NAME,拒绝。"
[[ "$ref" == "refs/heads/master" ]] || deny "ref '$ref' 不是 refs/heads/master。"
[[ " $allowed_workflows " == *" $workflow_ref "* ]] || deny "workflow '$workflow_ref' 不在白名单。"
[[ " $allowed_events " == *" $event "* ]] || deny "事件 '$event' 不在白名单($allowed_events)。"

printf 'runner guard: ✅ 放行 %s(%s)\n' "$workflow_ref" "$event"
