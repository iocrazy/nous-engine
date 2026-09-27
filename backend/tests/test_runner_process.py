"""Lane C: runner 子进程骨架测试 —— 真 multiprocessing.Process 起 fake runner.

用 spawn context（与 CUDA 子进程惯例一致）。runner 内跑 FakeAdapter，
不碰真 GPU。验证 Ready 握手 + LoadModel + RunNode + NodeProgress/NodeResult +
Abort-during-node。

Lane D 后：runner_main 用 ModelManager 替换 Lane C 极简 adapter dict；
kwarg `adapter_class` 已替换为 `fake_adapter=True` + `models_yaml_path`，
model_key 必须在 yaml fixture 里（"fake-img-a"/"fake-img-b"）。
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


def _spawn_runner(group_id="image", gpus=(2,)):
    """起一个 fake runner 子进程，返回 (process, PipeChannel 主进程侧)。"""
    parent_conn, child_conn = _SPAWN.Pipe()
    proc = _SPAWN.Process(
        target=runner_main,
        args=(group_id, list(gpus), child_conn),
        kwargs={"models_yaml_path": _FIXTURE, "fake_adapter": True},
        daemon=True,
    )
    proc.start()
    child_conn.close()  # 主进程侧不用 child 端
    return proc, PipeChannel(parent_conn)


@pytest.mark.asyncio
async def test_runner_sends_ready_on_startup():
    """子进程 event loop 起来后第一个发 Ready（runner_id + group + gpus）。"""
    proc, ch = _spawn_runner(group_id="image", gpus=(2,))
    try:
        msg = await _recv(ch)
        assert isinstance(msg, P.Ready)
        assert msg.group_id == "image"
        assert msg.gpus == [2]
        assert msg.runner_id  # 非空
    finally:
        await _shutdown(proc, ch)


@pytest.mark.asyncio
async def test_load_model_then_run_node():
    """LoadModel → ModelEvent(loaded)；RunNode → NodeProgress* → NodeResult(completed)。"""
    proc, ch = _spawn_runner()
    try:
        await _recv(ch)  # 吞掉 Ready
        # steps 经 LoadModel.config 给 FakeAdapter(AudioRequest 不带 steps)。
        await ch.send_message(P.LoadModel(model_key="fake-img-a", config={"steps": 3}))
        ev = await _recv(ch)
        assert isinstance(ev, P.ModelEvent) and ev.event == "loaded"

        await ch.send_message(P.RunNode(
            task_id=7, node_id="sampler", node_type="tts",
            model_key="fake-img-a", inputs={},
        ))
        progresses, result = await _collect_until_result(ch)
        assert len(progresses) == 3  # 每 step 一个 NodeProgress
        assert isinstance(result, P.NodeResult)
        assert result.status == "completed"
        assert result.task_id == 7
        assert result.outputs is not None
    finally:
        await _shutdown(proc, ch)


@pytest.mark.asyncio
async def test_ping_returns_pong():
    proc, ch = _spawn_runner()
    try:
        await _recv(ch)  # Ready
        await ch.send_message(P.Ping())
        pong = await _recv(ch)
        assert isinstance(pong, P.Pong)
        assert pong.runner_id
    finally:
        await _shutdown(proc, ch)


@pytest.mark.asyncio
async def test_load_failed_emits_model_event():
    """fail_load 的模型 —— ModelEvent(load_failed)，runner 不崩。"""
    proc, ch = _spawn_runner()
    try:
        await _recv(ch)  # Ready
        # config 里的 fail_load 透传给 FakeAdapter 构造
        await ch.send_message(P.LoadModel(
            model_key="fake-img-b", config={"fail_load": True},
        ))
        ev = await _recv(ch)
        assert isinstance(ev, P.ModelEvent)
        assert ev.event == "load_failed"
        assert ev.error
        # runner 仍活着：再 Ping 应回 Pong
        await ch.send_message(P.Ping())
        assert isinstance(await _recv(ch), P.Pong)
    finally:
        await _shutdown(proc, ch)


@pytest.mark.asyncio
async def test_abort_during_node_cancels_it():
    """RunNode 执行中收到 Abort —— 该节点 NodeResult.status == cancelled.

    pipe-reader 收 Abort 立即置 threading.Event；node-executor 的 fake adapter
    在下一 step 边界看到 flag → CancelledError → NodeResult(cancelled)。
    """
    proc, ch = _spawn_runner()
    try:
        await _recv(ch)  # Ready
        await ch.send_message(P.LoadModel(model_key="fake-img-a", config={"infer_seconds": 0.1, "steps": 20}))
        assert isinstance(await _recv(ch), P.ModelEvent)
        # 跑一个 20 step 的长节点
        await ch.send_message(P.RunNode(
            task_id=9, node_id="sampler", node_type="tts",
            model_key="fake-img-a", inputs={},
        ))
        # 收到第一个 progress 后立刻 Abort
        first = await _recv(ch)
        assert isinstance(first, P.NodeProgress)
        await ch.send_message(P.Abort(task_id=9, node_id="sampler"))
        # 继续收，最终应是 cancelled 的 NodeResult
        _, result = await _collect_until_result(ch, already=[first])
        assert isinstance(result, P.NodeResult)
        assert result.status == "cancelled"
        assert result.task_id == 9
    finally:
        await _shutdown(proc, ch)


# —— 测试辅助 ——


async def _recv(ch: PipeChannel, timeout: float = 10.0):
    return await asyncio.wait_for(ch.recv_message(), timeout=timeout)


async def _collect_until_result(ch: PipeChannel, already=None):
    """收消息直到拿到 NodeResult，返回 (progress 列表, NodeResult)。"""
    progresses = list(already or [])
    progresses = [m for m in progresses if isinstance(m, P.NodeProgress)]
    while True:
        msg = await _recv(ch)
        if isinstance(msg, P.NodeResult):
            return progresses, msg
        if isinstance(msg, P.NodeProgress):
            progresses.append(msg)


async def _shutdown(proc, ch: PipeChannel):
    ch.close()
    proc.join(timeout=5.0)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=3.0)
