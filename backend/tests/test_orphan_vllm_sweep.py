"""开机 vLLM 孤儿清扫的判定 —— spec 2026-09-05 §7 的第三条分支。

修前只有两条:不健康 → 杀;健康且能对上 registry spec → 接管。健康但**对不上任何
spec** 的那条是默默放着不管。3.6 退役后这条有牙了:后端崩溃重启(不是
`systemctl restart` —— 那会把子进程一起收)会留着 3.6 抱着 ~40G 在 GPU 0/2,
3.8 的常驻预加载过不了 `_assert_explicit_fits`,之后每个 LLM 请求都 503,且无日志。
"""
from __future__ import annotations

import signal

from src.api.main import _classify_orphan, _kill_orphan_vllm


def test_unhealthy_orphan_is_killed_regardless_of_spec_match():
    assert _classify_orphan(healthy=False, matched_spec=True) == "kill"
    assert _classify_orphan(healthy=False, matched_spec=False) == "kill"


def test_healthy_orphan_with_matching_spec_is_adopted():
    assert _classify_orphan(healthy=True, matched_spec=True) == "adopt"


def test_healthy_orphan_without_spec_is_killed_not_left_running():
    """本 PR 的行为改变:没人会接管它,它却抱着显存把常驻模型顶死。"""
    assert _classify_orphan(healthy=True, matched_spec=False) == "kill_unmatched"


def test_kill_orphan_targets_the_exact_pid_via_safe_signal(monkeypatch):
    """收进程只走 safe_killpg/safe_kill 的精确 PID 路径,绝不 `pkill -f`。"""
    import src.services.safe_signal as ss

    calls: list[tuple[str, int, int]] = []

    def _killpg(pid, sig, verify=None):
        calls.append(("killpg", pid, sig))
        return True

    monkeypatch.setattr(ss, "safe_killpg", _killpg)
    monkeypatch.setattr(ss, "safe_kill", lambda pid, sig: calls.append(("kill", pid, sig)))

    _kill_orphan_vllm(4242)
    assert calls == [("killpg", 4242, signal.SIGKILL)]


def test_kill_orphan_falls_back_to_single_pid_when_group_kill_refused(monkeypatch):
    import src.services.safe_signal as ss

    calls: list[tuple[str, int, int]] = []

    def _killpg_refused(pid, sig, verify=None):
        calls.append(("killpg", pid, sig))
        return False        # 非广播理由被拒 → 退回单 PID

    monkeypatch.setattr(ss, "safe_killpg", _killpg_refused)
    monkeypatch.setattr(ss, "safe_kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    monkeypatch.setattr(ss, "_proc_cmdline_contains", lambda pid, needle: True)

    _kill_orphan_vllm(4243)
    assert calls == [("killpg", 4243, signal.SIGKILL), ("kill", 4243, signal.SIGKILL)]
