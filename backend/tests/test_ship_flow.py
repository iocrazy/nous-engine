"""`infra/ship.sh` + `infra/autodeploy-guard.sh` 的行为测试(2026-09-09)。

ship.sh 是「CI 全绿 → 合并 → 上线」的一条命令,在 Mac 上跑;guard 是它经 ssh 灌到
GPU 机上执行的上线前闸门。两者都是 bash,这里用 PATH 桩(假 gh / ssh / curl)驱动,
断言的是**调用序列与退出码**,不是脚本文本。

退出码契约(ship.sh 头部注释是权威,这里锁住):
  0 已合并且已上线 / 2 用法错 / 3 CI 红 / 4 合并失败 / 5 已合并但闸门拒绝上线 /
  6 上线失败或上线后不含本 PR
guard:0 放行 / 5 拒绝。
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
SHIP = _REPO / "infra/ship.sh"
GUARD = _REPO / "infra/autodeploy-guard.sh"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


def _run(script: Path, args: list[str], env: dict[str, str], cwd: Path | None = None):
    full = {**os.environ, **env}
    return subprocess.run(
        ["bash", str(script), *args],
        env=full,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# ── guard ────────────────────────────────────────────────────────────────────


@pytest.fixture
def prod_dir(tmp_path: Path) -> Path:
    """一个长得像生产检出的目录:有 .nous-production 标记 + backend/.env 里的 ADMIN_TOKEN。"""
    d = tmp_path / "prod"
    (d / "backend").mkdir(parents=True)
    (d / ".nous-production").touch()
    (d / "backend/.env").write_text("ADMIN_TOKEN=tok-for-test\nOTHER=1\n", encoding="utf-8")
    return d


@pytest.fixture
def guard_bin(tmp_path: Path) -> Path:
    """假 curl:把 FAKE_HEALTH 原样吐出;FAKE_CURL_RC 非 0 时模拟连不上(无输出)。"""
    b = tmp_path / "bin"
    b.mkdir()
    _stub(
        b,
        "curl",
        'echo "$@" >> "$STUB_LOG"\n'
        'rc="${FAKE_CURL_RC:-0}"\n'
        '[[ "$rc" != 0 ]] && exit "$rc"\n'
        'printf "%s" "$FAKE_HEALTH"\n',
    )
    return b


def _guard(prod_dir: Path, guard_bin: Path, tmp_path: Path, **env: str):
    log = tmp_path / "stub.log"
    log.touch()
    base = {"PATH": f"{guard_bin}:{os.environ['PATH']}", "STUB_LOG": str(log)}
    r = _run(GUARD, [], {**base, **env}, cwd=prod_dir)
    return r, log.read_text(encoding="utf-8")


def test_guard_allows_when_idle(prod_dir, guard_bin, tmp_path):
    r, log = _guard(prod_dir, guard_bin, tmp_path, FAKE_HEALTH='{"online": true, "running_render": null}')
    assert r.returncode == 0, r.stdout + r.stderr
    # 探的是 comfy/health,且带了 .env 里的 ADMIN_TOKEN、绕过代理(本机跑着 mihomo)
    assert "/api/v1/comfy/health" in log
    assert "Bearer tok-for-test" in log
    assert "--noproxy" in log


def test_guard_refuses_while_render_running(prod_dir, guard_bin, tmp_path):
    r, _ = _guard(
        prod_dir,
        guard_bin,
        tmp_path,
        FAKE_HEALTH='{"online": true, "running_render": {"task_id": "t-42", "held_seconds": 1830}}',
    )
    assert r.returncode == 5
    assert "t-42" in r.stdout + r.stderr, "拒绝时要说出是哪个任务占着"


def test_guard_refuses_when_switched_off(prod_dir, guard_bin, tmp_path):
    (prod_dir / ".nous-autodeploy-off").touch()
    r, log = _guard(prod_dir, guard_bin, tmp_path, FAKE_HEALTH='{"running_render": null}')
    assert r.returncode == 5
    assert ".nous-autodeploy-off" in r.stdout + r.stderr
    assert log.strip() == "", "开关关着就不该再去探健康接口"


def test_guard_treats_unreachable_backend_as_idle(prod_dir, guard_bin, tmp_path):
    """后端本身没起来 → 没有渲染可保护,上线反而是修复,放行(但要说明)。"""
    r, _ = _guard(prod_dir, guard_bin, tmp_path, FAKE_CURL_RC="7", FAKE_HEALTH="")
    assert r.returncode == 0
    assert "不可达" in r.stdout + r.stderr


def test_guard_treats_garbage_health_as_idle(prod_dir, guard_bin, tmp_path):
    r, _ = _guard(prod_dir, guard_bin, tmp_path, FAKE_HEALTH="<html>502</html>")
    assert r.returncode == 0


def test_guard_refuses_outside_production_checkout(tmp_path, guard_bin):
    """没有 .nous-production 标记 = 不是生产检出,跟 deploy.sh 同一条防呆。"""
    d = tmp_path / "dev"
    (d / "backend").mkdir(parents=True)
    r, _ = _guard(d, guard_bin, tmp_path, FAKE_HEALTH='{"running_render": null}')
    assert r.returncode == 5
    assert ".nous-production" in r.stdout + r.stderr


# ── ship ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def ship_bin(tmp_path: Path) -> Path:
    """假 gh / ssh。每次调用把 argv 追加到 STUB_LOG(一行一个调用);ssh 还把 stdin 存下来。

    FAKE_PR_STATE      gh pr view 返回的 state(默认 OPEN)
    FAKE_PR_DRAFT      isDraft(默认 false)
    FAKE_PR_BASE       baseRefName(默认 master)
    FAKE_CHECKS_RC     gh pr checks 的退出码(默认 0 = 全绿)
    FAKE_MERGE_RC      gh pr merge 的退出码(默认 0)
    FAKE_SSH_GUARD_RC  灌 guard 的那次 ssh 的退出码(默认 0)
    FAKE_SSH_DEPLOY_RC 跑 deploy.sh 的那次 ssh 的退出码(默认 0)
    FAKE_SSH_VERIFY_RC 上线后 merge-base 校验的退出码(默认 0)
    """
    b = tmp_path / "bin"
    b.mkdir()
    _stub(
        b,
        "gh",
        'echo "gh $*" >> "$STUB_LOG"\n'
        'case "$1 $2" in\n'
        '  "auth status") exit 0 ;;\n'
        '  "pr view")\n'
        '    if [[ "$*" == *mergeCommit* ]]; then echo "deadbeefcafe"; exit 0; fi\n'
        '    printf \'{"state":"%s","isDraft":%s,"baseRefName":"%s","title":"t"}\' '
        '"${FAKE_PR_STATE:-OPEN}" "${FAKE_PR_DRAFT:-false}" "${FAKE_PR_BASE:-master}" ;;\n'
        '  "pr checks") exit "${FAKE_CHECKS_RC:-0}" ;;\n'
        '  "pr merge") exit "${FAKE_MERGE_RC:-0}" ;;\n'
        '  *) echo "gh stub: 未预期的调用 $*" >&2; exit 99 ;;\n'
        "esac\n",
    )
    _stub(
        b,
        "ssh",
        'echo "ssh $*" >> "$STUB_LOG"\n'
        'if [[ "$*" == *"bash -s"* ]]; then cat > "$STUB_LOG.guard-stdin"; exit "${FAKE_SSH_GUARD_RC:-0}"; fi\n'
        'if [[ "$*" == *deploy.sh* ]]; then exit "${FAKE_SSH_DEPLOY_RC:-0}"; fi\n'
        'if [[ "$*" == *merge-base* ]]; then exit "${FAKE_SSH_VERIFY_RC:-0}"; fi\n'
        'echo "ssh stub: 未预期的调用 $*" >&2; exit 99\n',
    )
    return b


def _ship(ship_bin: Path, tmp_path: Path, args: list[str], **env: str):
    log = tmp_path / "stub.log"
    log.touch()
    base = {
        "PATH": f"{ship_bin}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "NOUS_SHIP_HOST": "prodhost",
        "NOUS_SHIP_PROD_DIR": "/srv/nous-prod",
        "NOUS_SHIP_DISCORD_WEBHOOK": "",
    }
    r = _run(SHIP, args, {**base, **env})
    return r, log.read_text(encoding="utf-8").splitlines()


def test_ship_usage_without_pr(ship_bin, tmp_path):
    r, log = _ship(ship_bin, tmp_path, [])
    assert r.returncode == 2
    assert log == [], "用法错不该碰 gh"


def test_ship_happy_path_merges_then_guards_then_deploys(ship_bin, tmp_path):
    r, log = _ship(ship_bin, tmp_path, ["123"])
    assert r.returncode == 0, r.stdout + r.stderr
    kinds = [ln.split()[0] + " " + " ".join(ln.split()[1:3]) for ln in log]
    # 顺序是契约:先看 CI,再合,再闸门,再上线,再校验
    checks = next(i for i, ln in enumerate(log) if ln.startswith("gh pr checks"))
    merge = next(i for i, ln in enumerate(log) if ln.startswith("gh pr merge"))
    guard = next(i for i, ln in enumerate(log) if "bash -s" in ln)
    deploy = next(i for i, ln in enumerate(log) if "deploy.sh" in ln)
    verify = next(i for i, ln in enumerate(log) if "merge-base" in ln)
    assert checks < merge < guard < deploy < verify, "\n".join(log)
    assert any("--watch" in ln for ln in log if ln.startswith("gh pr checks")), "CI 未完成时要等,不是立刻判红"
    assert any("--squash" in ln and "--delete-branch" in ln for ln in log if ln.startswith("gh pr merge"))
    # ssh 一律非交互:不能卡在密码提示上
    assert all("BatchMode=yes" in ln for ln in log if ln.startswith("ssh"))
    # 闸门灌的是仓库里那份 guard(不依赖生产机上已有)
    assert "nous-autodeploy-off" in (tmp_path / "stub.log.guard-stdin").read_text(encoding="utf-8")
    # 上线后校验的是这次的 merge commit 在生产 HEAD 里
    assert any("deadbeefcafe" in ln for ln in log if "merge-base" in ln)
    assert "deadbeefcafe" in r.stdout


def test_ship_red_ci_never_merges(ship_bin, tmp_path):
    r, log = _ship(ship_bin, tmp_path, ["123"], FAKE_CHECKS_RC="1")
    assert r.returncode == 3
    assert not any(ln.startswith("gh pr merge") for ln in log)
    assert not any(ln.startswith("ssh") for ln in log)


@pytest.mark.parametrize(
    "env, why",
    [
        ({"FAKE_PR_STATE": "MERGED"}, "已合并的 PR"),
        ({"FAKE_PR_STATE": "CLOSED"}, "已关闭的 PR"),
        ({"FAKE_PR_DRAFT": "true"}, "draft"),
        ({"FAKE_PR_BASE": "develop"}, "不是对着 master"),
    ],
)
def test_ship_rejects_unshippable_pr(ship_bin, tmp_path, env, why):
    r, log = _ship(ship_bin, tmp_path, ["123"], **env)
    assert r.returncode == 2, why
    assert not any(ln.startswith("gh pr checks") for ln in log), f"{why}:不该走到 CI 检查"


def test_ship_merge_failure_stops_before_ssh(ship_bin, tmp_path):
    r, log = _ship(ship_bin, tmp_path, ["123"], FAKE_MERGE_RC="1")
    assert r.returncode == 4
    assert not any(ln.startswith("ssh") for ln in log)


def test_ship_guard_refusal_is_merged_but_not_deployed(ship_bin, tmp_path):
    r, log = _ship(ship_bin, tmp_path, ["123"], FAKE_SSH_GUARD_RC="5")
    assert r.returncode == 5
    assert any(ln.startswith("gh pr merge") for ln in log), "闸门是合并之后的事"
    assert not any("deploy.sh" in ln for ln in log)
    assert "已合并" in r.stdout + r.stderr and "未上线" in r.stdout + r.stderr


def test_ship_deploy_failure_exit_6(ship_bin, tmp_path):
    r, log = _ship(ship_bin, tmp_path, ["123"], FAKE_SSH_DEPLOY_RC="1")
    assert r.returncode == 6
    assert not any("merge-base" in ln for ln in log)


def test_ship_verify_failure_exit_6(ship_bin, tmp_path):
    """deploy.sh 说成功,但生产 HEAD 不含本 PR 的 merge commit → 也算失败,别报假成功。"""
    r, _ = _ship(ship_bin, tmp_path, ["123"], FAKE_SSH_VERIFY_RC="1")
    assert r.returncode == 6


def test_ship_no_deploy_flag_stops_after_merge(ship_bin, tmp_path):
    r, log = _ship(ship_bin, tmp_path, ["123", "--no-deploy"])
    assert r.returncode == 0
    assert any(ln.startswith("gh pr merge") for ln in log)
    assert not any(ln.startswith("ssh") for ln in log)


def test_ship_deploy_only_skips_gh_entirely(ship_bin, tmp_path):
    """闸门拒绝过一次之后的重试入口:不再合并,直接闸门 + 上线。"""
    r, log = _ship(ship_bin, tmp_path, ["--deploy-only"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert not any(ln.startswith("gh pr") for ln in log)
    assert any("bash -s" in ln for ln in log) and any("deploy.sh" in ln for ln in log)


# ── 静态守卫:ship 依赖的两个前提不能被别处顺手改掉 ────────────────────────────


def test_sudoers_still_allows_noninteractive_restart():
    """ssh 非交互里 sudo 没法输密码;deploy.sh 那句 --no-block restart 必须免密。"""
    text = (_REPO / "infra/security/nous-engine-deploy.sudoers").read_text(encoding="utf-8")
    assert "NOPASSWD: /usr/bin/systemctl --no-block restart nous-engine-backend" in text


def test_autodeploy_off_marker_is_gitignored():
    """标记落在生产检出根目录;不 ignore 会让 deploy.sh 的脏树守卫把它当未提交改动。"""
    assert ".nous-autodeploy-off" in (_REPO / ".gitignore").read_text(encoding="utf-8").split()
