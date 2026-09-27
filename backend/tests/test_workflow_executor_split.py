"""Lane S: WorkflowExecutor 节点分流执行测试。"""
import pytest

from src.services.workflow_executor import ExecutionError, WorkflowExecutor
from tests.fixtures.fake_runner_client import FakeRunnerClient


def _wf(nodes, edges=None):
    return {"nodes": nodes, "edges": edges or []}


def _collector():
    events: list[dict] = []

    async def _on(e: dict) -> None:
        events.append(e)

    return events, _on


@pytest.mark.asyncio
async def test_fake_runner_client_records_calls():
    """stub self-check：run_node 记录调用、按 node_id 返回配置结果。"""
    from src.runner import protocol as P
    rc = FakeRunnerClient(results={"n1": {"image_url": "x.png"}})
    spec = P.RunNode(task_id=1, node_id="n1", node_type="flux2_vae_decode",
                     model_key=None, inputs={"prompt": "cat"})
    out = await rc.run_node(spec, workflow_name="t1")
    assert out == {"image_url": "x.png"}
    assert rc.calls == [("n1", "flux2_vae_decode", {"prompt": "cat"}, "t1")]


@pytest.mark.asyncio
async def test_fake_runner_client_fail_nodes():
    from src.runner import protocol as P
    rc = FakeRunnerClient(fail_nodes={"bad"})
    spec = P.RunNode(task_id=2, node_id="bad", node_type="flux2_vae_decode",
                     model_key=None, inputs={})
    with pytest.raises(RuntimeError, match="node bad failed"):
        await rc.run_node(spec)


@pytest.mark.asyncio
async def test_inline_node_does_not_touch_runner_client():
    """纯 inline workflow：runner_client 一次都不该被调。"""
    rc = FakeRunnerClient()
    wf = _wf([{"id": "t1", "type": "text_input", "data": {"text": "hello"}}])
    ex = WorkflowExecutor(wf, runner_client=rc)
    result = await ex.execute()
    assert rc.calls == []
    assert "t1" in result["outputs"]


# 2026-09-26:以下 dispatch 用例原先拿 flux2_vae_decode 当样本节点;该节点随自建图像引擎
# 删除,现在唯一的 dispatch 节点是 tts_engine。用例改名重建,断言逐字保留。

@pytest.mark.asyncio
async def test_tts_dispatch_node_routes_to_runner_client():
    """tts_engine 节点 → RunnerClient.run_node，结果进 outputs。"""
    rc = FakeRunnerClient(results={"img": {"image_url": "out.png"}})
    wf = _wf([{"id": "img", "type": "tts_engine", "data": {"prompt": "cat"}}])
    ex = WorkflowExecutor(wf, runner_client=rc)
    result = await ex.execute()
    assert rc.calls[0][0] == "img"
    assert result["outputs"]["img"] == {"image_url": "out.png"}


@pytest.mark.asyncio
async def test_mixed_workflow_inline_then_tts_dispatch():
    """text_input(inline) → tts_engine(dispatch)：上游 inline 输出进下游 dispatch 的 inputs。"""
    rc = FakeRunnerClient(results={"img": {"image_url": "out.png"}})
    wf = _wf(
        nodes=[
            {"id": "t", "type": "text_input", "data": {"text": "a cat"}},
            {"id": "img", "type": "tts_engine", "data": {}},
        ],
        edges=[{"source": "t", "target": "img",
                "sourceHandle": "text", "targetHandle": "prompt"}],
    )
    ex = WorkflowExecutor(wf, runner_client=rc)
    result = await ex.execute()
    # dispatch 节点拿到了 inline 上游的输出
    assert "text" in rc.calls[0][2] or "prompt" in rc.calls[0][2]
    assert result["outputs"]["img"] == {"image_url": "out.png"}


@pytest.mark.asyncio
async def test_tts_dispatch_node_without_runner_client_raises():
    """runner_client=None 但 workflow 含 dispatch 节点 → ExecutionError（不静默 inline 跑 GPU 节点）。"""
    wf = _wf([{"id": "img", "type": "tts_engine", "data": {}}])
    ex = WorkflowExecutor(wf, runner_client=None)
    with pytest.raises(ExecutionError, match="runner"):
        await ex.execute()


@pytest.mark.asyncio
async def test_tts_dispatch_node_failure_wrapped():
    """runner 抛错 → ExecutionError，node_error progress 事件发出。"""
    events, on_progress = _collector()
    rc = FakeRunnerClient(fail_nodes={"img"})
    wf = _wf([{"id": "img", "type": "tts_engine", "data": {}}])
    ex = WorkflowExecutor(wf, runner_client=rc, on_progress=on_progress)
    with pytest.raises(ExecutionError):
        await ex.execute()
    assert any(e["type"] == "node_error" and e["node_id"] == "img" for e in events)


@pytest.mark.asyncio
async def test_progress_events_unchanged_for_tts_dispatch():
    """dispatch 节点同样发 node_start / node_complete progress 事件。"""
    events, on_progress = _collector()
    rc = FakeRunnerClient(results={"img": {"image_url": "x"}})
    wf = _wf([{"id": "img", "type": "tts_engine", "data": {}}])
    ex = WorkflowExecutor(wf, runner_client=rc, on_progress=on_progress)
    await ex.execute()
    types = [e["type"] for e in events]
    assert "node_start" in types and "node_complete" in types
