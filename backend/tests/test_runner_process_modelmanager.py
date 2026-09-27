"""Lane D: runner 子进程接 ModelManager —— 真 multiprocessing.Process.

验证：
  * runner 用 build_runner_model_manager 构造的真 ModelManager（fake adapter 模式）
  * LoadModel → ModelEvent(loaded)；RunNode → NodeResult
  * 并发的同模型 RunNode 被 per-model asyncio.Lock 串行化（核心验证点）
  * load_failed 不崩 runner
"""
import asyncio
import multiprocessing as mp
from pathlib import Path

import pytest

from src.runner import protocol as P
from src.runner.pipe_channel import PipeChannel
from src.runner.runner_process import runner_main

_SPAWN = mp.get_context("spawn")
_FIXTURE = str(Path(__file__).parent / "fixtures" / "runner_models.yaml")
# fake-img-a 带 params.steps=3:lazy-load 用例不发 LoadModel,只能经 yaml 给 FakeAdapter 多步。
_FIXTURE_MULTISTEP = str(Path(__file__).parent / "fixtures" / "runner_models_multistep.yaml")


def _spawn_runner(group_id="image", gpus=(2,), models_yaml_path=_FIXTURE):
    parent_conn, child_conn = _SPAWN.Pipe()
    proc = _SPAWN.Process(
        target=runner_main,
        args=(group_id, list(gpus), child_conn),
        kwargs={"models_yaml_path": models_yaml_path, "fake_adapter": True},
        daemon=True,
    )
    proc.start()
    child_conn.close()
    return proc, PipeChannel(parent_conn)


async def _recv(ch, timeout=10.0):
    return await asyncio.wait_for(ch.recv_message(), timeout=timeout)


async def _shutdown(proc, ch):
    ch.close()
    proc.join(timeout=5.0)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=3.0)


async def _collect_until_result(ch, task_id):
    """收消息直到拿到指定 task_id 的 NodeResult，返回 (progress 列表, result)。"""
    progresses = []
    while True:
        msg = await _recv(ch)
        if isinstance(msg, P.NodeResult) and msg.task_id == task_id:
            return progresses, msg
        if isinstance(msg, P.NodeProgress) and msg.task_id == task_id:
            progresses.append(msg)


@pytest.mark.asyncio
async def test_runner_loads_model_via_modelmanager():
    """LoadModel —— runner 用 ModelManager.load_model，发 ModelEvent(loaded)。"""
    proc, ch = _spawn_runner()
    try:
        assert isinstance(await _recv(ch), P.Ready)
        await ch.send_message(P.LoadModel(model_key="fake-img-a", config={}))
        ev = await _recv(ch)
        assert isinstance(ev, P.ModelEvent)
        assert ev.event == "loaded"
        assert ev.model_key == "fake-img-a"
    finally:
        await _shutdown(proc, ch)


@pytest.mark.asyncio
async def test_runner_run_node_through_get_or_load():
    """RunNode —— node-executor 走 ModelManager.get_or_load 拿 adapter 再 infer.

    不预先 LoadModel —— get_or_load 应 lazy load。
    """
    proc, ch = _spawn_runner(models_yaml_path=_FIXTURE_MULTISTEP)
    try:
        assert isinstance(await _recv(ch), P.Ready)
        await ch.send_message(P.RunNode(
            task_id=20, node_id="sampler", node_type="tts",
            model_key="fake-img-a", inputs={"steps": 3},
        ))
        progresses, result = await _collect_until_result(ch, 20)
        assert result.status == "completed"
        assert result.task_id == 20
        assert len(progresses) == 3
    finally:
        await _shutdown(proc, ch)


@pytest.mark.asyncio
async def test_runner_unknown_model_fails_node_not_runner():
    """get_or_load 撞 ModelNotFoundError —— 该节点 failed，runner 不崩。"""
    proc, ch = _spawn_runner()
    try:
        assert isinstance(await _recv(ch), P.Ready)
        await ch.send_message(P.RunNode(
            task_id=21, node_id="sampler", node_type="tts",
            model_key="no-such-model", inputs={"steps": 1},
        ))
        _, result = await _collect_until_result(ch, 21)
        assert result.status == "failed"
        assert result.error
        # runner 仍活着
        await ch.send_message(P.Ping())
        assert isinstance(await _recv(ch), P.Pong)
    finally:
        await _shutdown(proc, ch)


@pytest.mark.asyncio
async def test_concurrent_same_model_runs_are_serialized():
    """核心验证（spec §1.3 / §4.5）：并发的同模型 RunNode 被 per-model 锁串行化.

    一次性投 3 个同模型 RunNode（每个 steps 较多 → infer 有可观测耗时）。runner
    内 node-executor 是单 task 串行从队列取 —— 加上 ModelManager.load_model 的
    per-model asyncio.Lock，3 个节点的执行**不重叠**：每个节点的全部 NodeProgress
    应连续出现，不与另一节点的 progress 交错。
    """
    proc, ch = _spawn_runner()
    try:
        assert isinstance(await _recv(ch), P.Ready)
        task_ids = [30, 31, 32]
        for tid in task_ids:
            await ch.send_message(P.RunNode(
                task_id=tid, node_id="sampler", node_type="tts",
                model_key="fake-img-a", inputs={"steps": 6},
            ))
        order: list[int] = []
        results: dict[int, P.NodeResult] = {}
        while len(results) < 3:
            msg = await _recv(ch)
            if isinstance(msg, P.NodeProgress):
                order.append(msg.task_id)
            elif isinstance(msg, P.NodeResult):
                results[msg.task_id] = msg
                order.append(msg.task_id)
        # 全部 completed
        assert all(r.status == "completed" for r in results.values())
        # 串行化断言：order 里每个 task_id 的出现是连续的一段，不交错。
        segments = [order[0]]
        for tid in order[1:]:
            if tid != segments[-1]:
                segments.append(tid)
        assert len(segments) == 3, (
            f"同模型节点执行交错了，期望 3 段连续，实得 segments={segments} "
            f"(order={order})"
        )
        assert set(segments) == set(task_ids)
    finally:
        await _shutdown(proc, ch)


@pytest.mark.asyncio
async def test_load_failed_model_emits_model_event():
    """LoadModel 一个会 OOM-到底的模型 —— ModelEvent(load_failed)，runner 不崩.

    config 透传 oom_on_load_count=5 给 FakeAdapter（怎么试都 OOM）→
    ModelManager.get_or_load evict 后重试仍 OOM → load_failed。
    """
    proc, ch = _spawn_runner()
    try:
        assert isinstance(await _recv(ch), P.Ready)
        await ch.send_message(P.LoadModel(
            model_key="fake-img-b", config={"oom_on_load_count": 5},
        ))
        ev = await _recv(ch)
        assert isinstance(ev, P.ModelEvent)
        assert ev.event == "load_failed"
        assert ev.error
        # runner 仍活着
        await ch.send_message(P.Ping())
        assert isinstance(await _recv(ch), P.Pong)
    finally:
        await _shutdown(proc, ch)
