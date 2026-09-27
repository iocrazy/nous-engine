"""带 seed 的 dispatch 节点 is_deterministic=True (spec §3.3)。

is_deterministic 在 _dispatch_node 据 merged_inputs.seed 计算,与节点类型无关。
2026-09-26:原用例拿 flux2_vae_decode 当 dispatch 节点;该节点随自建图像引擎删除,
改用 tts_engine 重建(断言逐字保留)。嵌套 latent["seed"] 的两条是 flux2 专属形态,删。
"""
from __future__ import annotations

import pytest

from src.runner import protocol as P
from src.services.workflow_executor import WorkflowExecutor


class _CapturingClient:
    def __init__(self):
        self.spec: P.RunNode | None = None

    async def run_node(self, spec, *, on_progress=None, workflow_name=""):
        self.spec = spec
        return P.NodeResult(task_id=spec.task_id, node_id=spec.node_id, status="completed",
                            outputs={"image_url": "u"}, error=None, duration_ms=1)


def _exec_tts(node_data):
    wf = {"nodes": [{"id": "g", "type": "tts_engine", "data": node_data}], "edges": []}
    client = _CapturingClient()
    ex = WorkflowExecutor(wf, runner_clients={"tts": client}, task_id=7)
    return ex, client


@pytest.mark.asyncio
async def test_tts_seed_sets_deterministic():
    ex, client = _exec_tts({"seed": 42})
    await ex._dispatch_node(ex._node_map["g"], {"seed": 42})
    assert client.spec.is_deterministic is True


@pytest.mark.asyncio
async def test_tts_no_seed_not_deterministic():
    ex, client = _exec_tts({})
    await ex._dispatch_node(ex._node_map["g"], {})
    assert client.spec.is_deterministic is False


@pytest.mark.asyncio
async def test_tts_empty_seed_not_deterministic():
    ex, client = _exec_tts({"seed": ""})
    await ex._dispatch_node(ex._node_map["g"], {"seed": ""})
    assert client.spec.is_deterministic is False
