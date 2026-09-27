"""PR-6: node_complete event carries cached flag from runner outputs."""
from __future__ import annotations

import pytest

from src.runner import protocol as P
from src.services.workflow_executor import WorkflowExecutor


class _Client:
    async def run_node(self, spec, *, on_progress=None, workflow_name=""):
        return P.NodeResult(task_id=spec.task_id, node_id=spec.node_id, status="completed",
                            outputs={"image_url": "u", "cached": True}, error=None, duration_ms=3)


# 2026-09-26:原用例拿 flux2_vae_decode(image 组)当 dispatch 节点;该节点随自建图像引擎
# 删除,改用 tts_engine(tts 组)重建,断言逐字保留。
@pytest.mark.asyncio
async def test_tts_node_complete_includes_cached():
    events = []
    async def on_prog(e): events.append(e)
    wf = {"nodes": [{"id": "g", "type": "tts_engine", "data": {"seed": 1}}], "edges": []}
    ex = WorkflowExecutor(wf, on_progress=on_prog, runner_clients={"tts": _Client()}, task_id=5)
    await ex.execute()
    complete = [e for e in events if e.get("type") == "node_complete" and e["node_id"] == "g"]
    assert complete and complete[-1].get("cached") is True
