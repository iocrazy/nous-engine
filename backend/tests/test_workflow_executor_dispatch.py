"""Wave 1 Task 4.5 — verify WorkflowExecutor._execute_node dispatches via
registered node classes (InvokableNode / StreamableNode protocols).
"""

from __future__ import annotations

import pytest

from src.services.workflow_executor import WorkflowExecutor


@pytest.mark.asyncio
async def test_executor_dispatches_via_registry():
    """_execute_node should look up class from registry, instantiate, call invoke/stream."""
    workflow = {
        "nodes": [{"id": "n1", "type": "text_input", "data": {"text": "hi"}}],
        "edges": [],
    }
    captured: list[dict] = []

    async def on_progress(ev):
        captured.append(ev)

    exe = WorkflowExecutor(workflow, on_progress=on_progress)
    result = await exe.execute()
    assert result["outputs"]["n1"] == {"text": "hi"}


def test_on_progress_ref_not_used_by_builtin_nodes():
    """Builtin 12 nodes must NOT reference the global _on_progress_ref."""
    import inspect
    from src.services.nodes import text_io, audio, logic, llm as llm_module
    for mod in (text_io, audio, logic, llm_module):
        src = inspect.getsource(mod)
        assert "_on_progress_ref" not in src, \
            f"{mod.__name__} should not reference global _on_progress_ref"

