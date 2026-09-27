"""`infra/runner/job-started-guard.sh` —— self-hosted 生产 runner 的硬闸(2026-09-27)。

runner 在每个 job 开始前执行它(ACTIONS_RUNNER_HOOK_JOB_STARTED),非 0 退出 = job 失败、
一行用户代码都不跑。public 仓库挂 self-hosted runner 的真实风险是 fork PR **自带**一个
`on: pull_request` + `runs-on: [self-hosted, …]` 的 workflow;GitHub 的 fork 审批是软闸,
这个 hook 是装在仓库之外的硬闸:只放行白名单里的 workflow 文件 @ master + 白名单事件。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / "infra/runner/job-started-guard.sh"

DEPLOY = "iocrazy/nous-engine/.github/workflows/deploy.yml@refs/heads/master"


def _run(**env: str) -> subprocess.CompletedProcess:
    base = {
        "PATH": os.environ["PATH"],
        "RUNNER_GUARD_ALLOWED_WORKFLOWS": DEPLOY,
        "RUNNER_GUARD_ALLOWED_EVENTS": "workflow_run workflow_dispatch",
        "GITHUB_WORKFLOW_REF": DEPLOY,
        "GITHUB_EVENT_NAME": "workflow_run",
        "GITHUB_REF": "refs/heads/master",
    }
    base.update(env)
    return subprocess.run(["bash", str(GUARD)], env=base, capture_output=True, text=True)


def test_allows_deploy_workflow_on_master():
    r = _run()
    assert r.returncode == 0, r.stderr
    r = _run(GITHUB_EVENT_NAME="workflow_dispatch")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("override", [
    # fork PR 自带的 workflow:文件不在白名单
    {"GITHUB_WORKFLOW_REF": "evil/nous-engine/.github/workflows/pwn.yml@refs/pull/9/merge",
     "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/9/merge"},
    # 同名文件,但来自 PR 的 merge ref(改过的 deploy.yml)
    {"GITHUB_WORKFLOW_REF": "iocrazy/nous-engine/.github/workflows/deploy.yml@refs/pull/9/merge",
     "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/9/merge"},
    # 白名单 workflow,但事件不对
    {"GITHUB_EVENT_NAME": "pull_request_target"},
    {"GITHUB_EVENT_NAME": "push"},
    # workflow_dispatch 选了别的分支
    {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/feature-x",
     "GITHUB_WORKFLOW_REF": "iocrazy/nous-engine/.github/workflows/deploy.yml@refs/heads/feature-x"},
    # 缺上下文一律拒
    {"GITHUB_WORKFLOW_REF": ""},
    {"GITHUB_EVENT_NAME": ""},
])
def test_denies_everything_else(override):
    r = _run(**override)
    assert r.returncode != 0
    assert "runner guard" in r.stderr


def test_unconfigured_guard_fails_closed():
    r = _run(RUNNER_GUARD_ALLOWED_WORKFLOWS="")
    assert r.returncode != 0


def test_multiple_allowed_workflows():
    other = "iocrazy/nous-app/.github/workflows/run-migration.yml@refs/heads/master"
    r = _run(RUNNER_GUARD_ALLOWED_WORKFLOWS=f"{DEPLOY} {other}",
             RUNNER_GUARD_ALLOWED_EVENTS="push schedule workflow_dispatch",
             GITHUB_WORKFLOW_REF=other, GITHUB_EVENT_NAME="schedule")
    assert r.returncode == 0, r.stderr
