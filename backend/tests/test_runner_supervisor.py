"""Lane C: RunnerSupervisor 测试 —— spawn / watchdog / crash 重启 / GPU-free gate.

用 fake runner 子进程 + 注入的 GPU-free 探针（不碰 nvidia-smi）。

Lane D 后：supervisor 从 adapter_class 转 fake_adapter + models_yaml_path。
"""
import asyncio
from pathlib import Path

import pytest

from src.runner import protocol as P
from src.runner.supervisor import RunnerSupervisor

_FIXTURE = str(Path(__file__).parent / "fixtures" / "runner_models.yaml")


def _make_supervisor(**overrides) -> RunnerSupervisor:
    """构造一个跑 fake runner 的 supervisor，超时参数缩小以便快测。"""
    kw = dict(
        group_id="image",
        gpus=[2],
        models_yaml_path=_FIXTURE,
        fake_adapter=True,
        ping_interval=0.3,
        ping_timeout=0.5,
        restart_backoff=[0.1, 0.2, 0.3],
        gpu_free_probe=lambda gpus: True,  # 默认 GPU 立即 free
    )
    kw.update(overrides)
    return RunnerSupervisor(**kw)


@pytest.mark.asyncio
async def test_start_spawns_runner_and_handshakes():
    sup = _make_supervisor()
    try:
        await sup.start()
        assert sup.is_running
        assert sup.client.is_ready
        # 能正常派活
        await sup.client.load_model("fake-img-a", config={})
        result = await sup.client.run_node(P.RunNode(
            task_id=1, node_id="n", node_type="tts",
            model_key="fake-img-a", inputs={"steps": 2},
        ))
        assert result.status == "completed"
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_busy_runner_alive_not_killed_on_ping_timeout(monkeypatch):
    """根治 ping-timeout 误杀:长 dispatch 阻塞事件循环 → ping 超时,但进程活着 = 忙,
    绝不能 kill(这正是 anima/长 denoise 被误杀的根因)。"""
    sup = _make_supervisor(max_unresponsive_seconds=300.0)
    try:
        await sup.start()
        assert sup._process.is_alive()

        async def _slow_ping():  # 模拟事件循环被占住,Pong 永远不来(但进程活着)
            await asyncio.sleep(10)

        monkeypatch.setattr(sup.client, "ping", _slow_ping)
        # 跑几个 watchdog 周期(ping_interval=0.3 + ping_timeout=0.5)
        await asyncio.sleep(1.6)
        assert sup.restart_count == 0  # 忙 runner 没被误杀
        assert sup.is_running
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_alive_but_unresponsive_too_long_restarts(monkeypatch):
    """死锁兜底:进程活着但持续无响应超过 max_unresponsive_seconds(疑死锁)→ 重启。
    没有图/音生成要这么久,所以阈值高于任何正常 dispatch 即安全。"""
    sup = _make_supervisor(max_unresponsive_seconds=0.5)
    try:
        await sup.start()

        async def _slow_ping():
            await asyncio.sleep(10)

        monkeypatch.setattr(sup.client, "ping", _slow_ping)
        await asyncio.wait_for(sup.wait_restarted(count=1), timeout=15.0)
        assert sup.restart_count >= 1  # 持续无响应 → 兜底重启
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_supervisor_stores_loaded_snapshot_from_pong():
    """Bug 3:runner 子进程加载的模型,主进程靠 Pong 上报的结构化快照可见。
    load_model 后对账一次 → sup.loaded_models 反映 runner 里的 _models。"""
    sup = _make_supervisor()
    try:
        await sup.start()
        assert sup.loaded_models == []  # start 时 runner 还没 load 任何模型
        await sup.client.load_model("fake-img-a", config={})
        await sup._reconcile_loaded()  # ping → Pong(loaded_models snapshot)
        ids = {e["model_id"] for e in sup.loaded_models}
        assert "fake-img-a" in ids
        e = next(e for e in sup.loaded_models if e["model_id"] == "fake-img-a")
        assert "model_type" in e and "gpu_index" in e and "source_files" in e
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_node_done_triggers_live_reconcile():
    """Bug 3 PR-2b:节点跑完(on_node_done)自动 reconcile 快照,不用等 30s watchdog ping
    也不用手动调 —— 模型加载后跑一个节点,sup.loaded_models 应即时出现该模型。"""
    sup = _make_supervisor()
    try:
        await sup.start()
        await sup.client.load_model("fake-img-a", config={})
        assert sup.loaded_models == []  # load_model 不触发 reconcile(只有 NodeResult 才)
        await sup.client.run_node(P.RunNode(
            task_id=99, node_id="n", node_type="tts",
            model_key="fake-img-a", inputs={"steps": 2}))
        # on_node_done → create_task(reconcile);轮询等它跑完(ping 往返很快)
        for _ in range(40):
            if any(e["model_id"] == "fake-img-a" for e in sup.loaded_models):
                break
            await asyncio.sleep(0.05)
        assert any(e["model_id"] == "fake-img-a" for e in sup.loaded_models)
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_restart_clears_loaded_snapshot():
    """crash 重启 → 旧 runner 的已加载快照随进程没了,sup.loaded_models 清空。"""
    sup = _make_supervisor()
    try:
        await sup.start()
        await sup.client.load_model("fake-img-a", config={})
        await sup._reconcile_loaded()
        assert sup.loaded_models  # 非空
        sup._process.terminate()
        await asyncio.wait_for(sup.wait_restarted(count=1), timeout=15.0)
        assert sup.loaded_models == []  # 重启后(还没新 ping)已清空
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_watchdog_detects_crash_and_restarts():
    """杀掉 runner 子进程 → watchdog ping 超时 → 自动重启 → 新 runner 可用。"""
    sup = _make_supervisor()
    try:
        await sup.start()
        old_pid = sup.pid
        # 模拟 crash
        sup._process.terminate()
        # 等 watchdog 检测 + 重启（ping_interval + ping_timeout + backoff + 重启）
        await asyncio.wait_for(sup.wait_restarted(count=1), timeout=15.0)
        assert sup.is_running
        assert sup.pid != old_pid  # 新进程
        assert sup.restart_count == 1
        # 新 runner 能干活
        await sup.client.load_model("fake-img-a", config={})
        result = await sup.client.run_node(P.RunNode(
            task_id=2, node_id="n", node_type="tts",
            model_key="fake-img-a", inputs={"steps": 2},
        ))
        assert result.status == "completed"
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_crash_marks_inflight_tasks_failed():
    """runner crash 时，supervisor 把登记的 inflight task 全标 failed（runner_crashed）。"""
    failed: list[tuple[int, str]] = []
    sup = _make_supervisor(
        on_task_failed=lambda task_id, reason: failed.append((task_id, reason)),
    )
    try:
        await sup.start()
        # 登记两个 inflight task
        sup.register_inflight(101)
        sup.register_inflight(102)
        sup._process.terminate()
        await asyncio.wait_for(sup.wait_restarted(count=1), timeout=15.0)
        assert sorted(t for t, _ in failed) == [101, 102]
        assert all(reason == "runner_crashed" for _, reason in failed)
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_restart_backoff_sequence():
    """连续 crash —— backoff 按 restart_backoff 序列递增，封顶最后一个值。"""
    sup = _make_supervisor(restart_backoff=[0.1, 0.3, 0.5])
    try:
        await sup.start()
        # backoff_for(n) 给第 n 次重启该等多久
        assert sup.backoff_for(0) == 0.1
        assert sup.backoff_for(1) == 0.3
        assert sup.backoff_for(2) == 0.5
        assert sup.backoff_for(3) == 0.5  # 封顶
        assert sup.backoff_for(99) == 0.5
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_gpu_free_gate_blocks_restart_until_clear():
    """GPU-free gate：探针返回 False 时重启被卡住，返回 True 后才继续（F2）。"""
    gate_state = {"free": False}
    probe_calls: list[int] = []

    def _probe(gpus):
        probe_calls.append(1)
        return gate_state["free"]

    sup = _make_supervisor(gpu_free_probe=_probe, gpu_free_poll_interval=0.1)
    try:
        await sup.start()
        sup._process.terminate()
        # gate 卡住 —— 0.5s 内不应完成重启
        await asyncio.sleep(1.0)
        assert not sup.is_running or sup.restart_count == 0
        assert len(probe_calls) >= 2  # gate 在轮询
        # 放行
        gate_state["free"] = True
        await asyncio.wait_for(sup.wait_restarted(count=1), timeout=15.0)
        assert sup.is_running
        assert sup.restart_count == 1
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_spawn_handshake_failure_terminates_orphan(monkeypatch):
    """round10:_spawn 的握手(client.start)失败时,已 fork 的子进程必须被终结。

    否则初始 start() 路径(watchdog 还没建)会留下 orphan runner 永久占着 GPU 显存,
    没人回收。"""
    import src.runner.supervisor as sup_mod

    async def _boom(self):
        raise asyncio.TimeoutError("ready handshake timeout")

    monkeypatch.setattr(sup_mod.RunnerClient, "start", _boom)

    sup = _make_supervisor()
    with pytest.raises(asyncio.TimeoutError):
        await sup._spawn()

    # 子进程已被 _spawn 主动终结,没泄漏
    assert sup._process is not None
    assert not sup._process.is_alive()
