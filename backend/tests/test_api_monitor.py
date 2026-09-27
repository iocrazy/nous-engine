import pytest
from httpx import AsyncClient
from unittest.mock import patch

# 本文件统一走 pytest-asyncio(pyproject 里 asyncio_mode=auto,async 测试自动挂 mark,
# 不需要显式 pytestmark —— 模块级 mark 会连同步测试一起标上,pytest-asyncio 会为每个
# 同步测试报一条 "is marked with '@pytest.mark.asyncio' but it is not an async function")。
# 别改用 anyio:混用会各建一个事件循环,asyncpg 的连接绑 loop 就抛 "attached to a
# different loop"(aiosqlite 不在意,所以以前看不出来)。


def test_monitor_loaded_models_from_model_manager():
    """monitor 端点的 loaded_models 来自 app.state.model_manager，不依赖 model_scheduler。"""
    import src.api.routes.monitor as monitor_mod

    # model_scheduler 模块被删后，monitor.py 不应再 import 它
    src = monitor_mod.__file__
    with open(src) as f:
        content = f.read()
    assert "model_scheduler" not in content, "monitor.py 不应再引用 model_scheduler"


# ----- PR-10: _gpu_processes managed/orphan detection + command_full -----


def test_gpu_processes_emits_command_full_and_short(monkeypatch):
    """nvidia-smi 列出的进程要带 `command_full`(hover tooltip) + 截断的 `command`。"""
    import subprocess
    from src.api.routes import monitor as m

    long_cmdline = [
        "/media/heygo/Program/projects-code/_playground/nous-center/backend/.venv/bin/python3",
        "-c", "from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=5, pipe_handle=25)",
        "--multiprocessing-fork", "extra1", "extra2", "extra3", "extra4", "extra5",
    ]

    class _FakeProc:
        def __init__(self, pid): self.pid = pid
        def name(self): return "python3"
        def cmdline(self): return long_cmdline
        def parents(self): return []

    def _fake_run(cmd, **kw):
        class R:
            pass
        r = R()
        r.returncode = 0
        # cmd 是 list,元素形如 `--query-compute-apps=gpu_uuid,pid,used_memory` —
        # 完整匹配 prefix 而不是直接 `in cmd`(list 是 exact-element 比对会全 miss)。
        if any("--query-compute-apps" in c for c in cmd):
            r.stdout = "GPU-uuid-1, 999, 1024\n"
        else:  # --query-gpu=index,uuid
            r.stdout = "1, GPU-uuid-1\n"
        return r

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(m.psutil, "Process", _FakeProc)

    result = m._gpu_processes(pid_map={})
    assert 1 in result
    proc = result[1][0]
    assert proc["pid"] == 999
    assert proc["command_full"] == " ".join(long_cmdline)
    # short command 截断到 ≤120 字符,且只取前 8 段
    assert len(proc["command"]) <= 120
    assert proc["command"].count(" ") <= 7 or proc["command"].endswith(("…", " "))


def test_gpu_processes_ancestor_walk_marks_grandchild_as_managed(monkeypatch):
    """multiprocessing.spawn 的 grandchild PID 不在 pid_map,但 parent 在 → 仍算 managed。

    用户报告:RunnerSupervisor._process.pid = 2501890,实际跑 GPU 任务的 grandchild
    PID = 2501948,被 UI 误标 orphan(红色 + kill 按钮)。本测试钉住 ancestor walk。
    """
    import subprocess
    from src.api.routes import monitor as m

    class _Parent:
        pid = 2501890

    class _FakeProc:
        def __init__(self, pid): self.pid = pid
        def name(self): return "python3"
        def cmdline(self): return ["python3", "-c", "spawn_main"]
        def parents(self): return [_Parent()]

    def _fake_run(cmd, **kw):
        class R:
            pass
        r = R()
        r.returncode = 0
        if any("--query-compute-apps" in c for c in cmd):
            r.stdout = "GPU-uuid-1, 2501948, 38000\n"
        else:
            r.stdout = "1, GPU-uuid-1\n"
        return r

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(m.psutil, "Process", _FakeProc)

    # 只 supervisor PID(parent) 在 pid_map,grandchild 不在
    result = m._gpu_processes(pid_map={2501890: "runner:image"})
    proc = result[1][0]
    assert proc["managed"] is True, "grandchild 应通过 ancestor walk 算 managed"
    assert proc["model_name"] == "runner:image"


def test_gpu_processes_orphan_when_no_ancestor_match(monkeypatch):
    """无任何 ancestor 在 pid_map → 仍算 orphan(ComfyUI 等外部进程)。"""
    import subprocess
    from src.api.routes import monitor as m

    class _UnrelatedParent:
        pid = 99999

    class _FakeProc:
        def __init__(self, pid): self.pid = pid
        def name(self): return "python3"
        def cmdline(self): return ["/home/x/ComfyUI/.venv/bin/python3", "main.py"]
        def parents(self): return [_UnrelatedParent()]

    def _fake_run(cmd, **kw):
        class R:
            pass
        r = R()
        r.returncode = 0
        if any("--query-compute-apps" in c for c in cmd):
            r.stdout = "GPU-uuid-1, 2865731, 26000\n"
        else:
            r.stdout = "1, GPU-uuid-1\n"
        return r

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(m.psutil, "Process", _FakeProc)

    result = m._gpu_processes(pid_map={2501890: "runner:image"})
    proc = result[1][0]
    assert proc["managed"] is False
    assert proc["model_name"] is None


@pytest.fixture(autouse=True)
def _clear_stats_cache():
    """/monitor/stats 有 1.5s TTL 缓存;测试间清掉,否则前一个测试的 mock 结果
    在 TTL 内被后一个测试串用(性能 P0 缓存的副作用)。"""
    from src.api.routes import monitor as _m
    _m._reset_stats_cache()
    yield
    _m._reset_stats_cache()


async def test_monitor_stats_ttl_cache_dedupes_rapid_polls(db_client: AsyncClient):
    """性能 P0:1.5s 内的重复轮询(前端双 query key)命中缓存,底层采集只跑一次。"""
    calls = {"n": 0}

    def _counting_gpu():
        calls["n"] += 1
        return []

    with patch("src.api.routes.monitor._gpu_stats_nvidia_smi", side_effect=_counting_gpu), \
         patch("src.api.routes.monitor._gpu_processes", return_value={}), \
         patch("src.api.routes.monitor._top_processes", return_value=[]):
        r1 = await db_client.get("/api/v1/monitor/stats")
        r2 = await db_client.get("/api/v1/monitor/stats")
        r3 = await db_client.get("/api/v1/monitor/stats")
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert calls["n"] == 1, f"expected 1 collection, got {calls['n']} (cache miss)"


async def test_monitor_stats_recomputes_after_ttl(db_client: AsyncClient, monkeypatch):
    """TTL 过期后重新采集。用 TTL=0 强制每次都 miss。"""
    from src.api.routes import monitor as _m
    monkeypatch.setattr(_m, "_STATS_TTL_S", 0.0)
    calls = {"n": 0}

    def _counting_gpu():
        calls["n"] += 1
        return []

    with patch("src.api.routes.monitor._gpu_stats_nvidia_smi", side_effect=_counting_gpu), \
         patch("src.api.routes.monitor._gpu_processes", return_value={}), \
         patch("src.api.routes.monitor._top_processes", return_value=[]):
        await db_client.get("/api/v1/monitor/stats")
        await db_client.get("/api/v1/monitor/stats")
    assert calls["n"] == 2, f"TTL=0 应每次重算,got {calls['n']}"
