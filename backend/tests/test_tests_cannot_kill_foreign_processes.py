"""2026-09-22 事故回归:测试进程把本机**生产** vLLM 杀了一整夜。

`with TestClient(app)` 跑真 lifespan → 启动期「孤儿 vLLM 清扫」扫真机 /proc → 看见
生产后端起的 vLLM → SIGKILL(对不上 spec)或接管后随测试 lifespan 关闭一并收掉。
生产侧只看得到「端口不可达 → 看门狗自愈」和退出码 -9,kill 日志在 pytest 输出里。

两道防线各一组用例:
1. conftest 的物理护栏:测试进程只准给自己的后代发真信号。
2. main.py 的开关:NOUS_DISABLE_ORPHAN_SWEEP=1 时启动期根本不扫真机进程。
"""
import os
import signal
import subprocess
import sys

import pytest


def test_guard_refuses_signal_to_non_descendant():
    """父进程(pytest 的启动者 / xdist 主控)不是后代 → 必须拦。

    故意用 SIGCONT:对运行中的进程是无害的空操作 —— 就算护栏坏了、信号真投递出去,
    也不会伤到任何东西。
    """
    with pytest.raises(AssertionError, match="不是本测试进程的后代"):
        os.kill(os.getppid(), signal.SIGCONT)
    with pytest.raises(AssertionError, match="不是本测试进程的后代"):
        os.killpg(os.getpgid(os.getppid()), signal.SIGCONT)


def test_guard_allows_signal_to_own_child_even_in_new_session():
    """自己 spawn 的子进程要能正常清理 —— 包括 start_new_session=True 的
    (vLLM 适配器就是这么起子进程的:换 session/pgid,不换 ppid)。"""
    p = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        os.killpg(p.pid, signal.SIGTERM)
        assert p.wait(timeout=10) == -signal.SIGTERM
    finally:
        if p.poll() is None:
            p.kill()


def test_guard_allows_probe_signal_zero():
    """kill(pid, 0) 只是探活,不投递信号 —— 对任何 pid 都放行。"""
    os.kill(os.getppid(), 0)


def test_lifespan_does_not_scan_host_vllm_processes(monkeypatch):
    """走 lifespan 的测试绝不能扫真机 vLLM —— 扫到就会按孤儿判定去杀生产进程。"""
    from fastapi.testclient import TestClient

    from src.api.main import create_app
    from src.services.inference import vllm_scanner

    assert os.environ.get("NOUS_DISABLE_ORPHAN_SWEEP") == "1", "conftest 必须置这个开关"

    calls = []
    monkeypatch.setattr(vllm_scanner, "scan_running_vllm", lambda: calls.append(1) or [])
    app = create_app()
    with TestClient(app):
        pass
    assert calls == [], "测试 lifespan 扫了真机 vLLM 进程 —— 会把生产模型当孤儿杀掉"
