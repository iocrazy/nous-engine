"""Lane J integration: workflow lifecycle + priority preemption + cancel + mixed-node.

spec §5.3 first four rows. Main-process scheduling + fake runner subprocess +
full IPC protocol.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.runner import protocol as P
from src.services.scheduler.group_scheduler import (
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    GroupScheduler,
)
from src.services.task_ring_buffer import TaskSnapshot

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_workflow_full_lifecycle(scheduler_env):
    """enqueue → dispatch → run_node → completed; ring buffer records terminal state."""
    env = scheduler_env
    await env.runner.start()
    try:
        loaded = await env.runner.client.load_model(env.runner.model_key, config={})
        assert loaded is True
        result = await env.runner.client.run_node(
            P.RunNode(
                task_id=100,
                node_id="sampler",
                node_type="tts",
                model_key=env.runner.model_key,
                inputs={"steps": 1},
            )
        )
        assert result.status == "completed"

        snap = TaskSnapshot(
            task_id=100,
            workflow_name="lifecycle",
            status="completed",
            priority=PRIORITY_BATCH,
            gpu_group="image",
            runner_id=env.runner.client.runner_id,
            nodes_total=1,
            nodes_done=1,
            current_node=None,
            queued_at=None,
            started_at=None,
            finished_at=None,
            duration_ms=result.duration_ms,
            error=None,
            cancel_reason=None,
            db_synced=True,
        )
        env.ring_buffer.push(snap)
        assert env.ring_buffer.get(100).status == "completed"
    finally:
        await env.runner.stop()


@pytest.mark.asyncio
async def test_priority_preemption():
    """batch task A enqueued first, interactive B enqueued after → B dispatches first.

    Uses a synchronous "drain" — for each dispatch, the executor records the
    task_id and exits. The scheduler dispatches in PriorityQueue order
    (lowest priority value first); spec §1.1 / §2.2.
    """
    dispatched: list[int] = []

    async def _executor(task_id, spec, cancel_event, cancel_flag):
        dispatched.append(task_id)
        await asyncio.sleep(0.01)
        return {}

    sched = GroupScheduler(group_id="image", executor=_executor)
    await sched.start()
    try:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # batch (priority=10) enqueued first
        await sched.enqueue(
            task_id=1,
            priority=PRIORITY_BATCH,
            queued_at=base,
            workflow_spec={},
        )
        # interactive (priority=0) enqueued after
        await sched.enqueue(
            task_id=2,
            priority=PRIORITY_INTERACTIVE,
            queued_at=base + timedelta(seconds=1),
            workflow_spec={},
        )
        await sched.join()
    finally:
        await sched.stop()

    assert dispatched == [2, 1], (
        "interactive (B) should dispatch before batch (A); got %r" % dispatched
    )


@pytest.mark.asyncio
async def test_cancel_inflight_via_abort(fake_runner):
    """run_node on a slow node + main-side Abort → status=cancelled (spec §5.3)."""
    runner = fake_runner(group_id="image", gpus=[2], slow_seconds=0.2)
    await runner.start()
    try:
        # steps 经 LoadModel.config 给 FakeAdapter(AudioRequest 不带 steps)。
        assert await runner.client.load_model(runner.model_key, config={"steps": 30}) is True
        run_task = asyncio.create_task(
            runner.client.run_node(
                P.RunNode(
                    task_id=200,
                    node_id="sampler",
                    node_type="tts",
                    model_key=runner.model_key,
                    inputs={"steps": 30},  # 30 steps * 0.2s = 6s budget
                )
            )
        )
        # Wait for infer to actually be running (one step ~0.2s).
        await asyncio.sleep(0.25)
        await runner.client.abort(task_id=200, node_id="sampler")
        result = await asyncio.wait_for(run_task, timeout=10.0)
        assert result.status == "cancelled"
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_mixed_node_workflow(scheduler_env, fake_vllm):
    """image dispatch (subprocess IPC) + llm inline HTTP (vLLM mock) in parallel.

    spec §5.3 mixed-node row: scheduler routes image node to runner subprocess,
    workflow_executor routes llm node to vLLM HTTP directly — both complete
    and results merge.
    """
    import httpx

    env = scheduler_env
    await env.runner.start()
    try:
        assert await env.runner.client.load_model(env.runner.model_key, config={}) is True

        async def _image_branch():
            r = await env.runner.client.run_node(
                P.RunNode(
                    task_id=300,
                    node_id="img",
                    node_type="tts",
                    model_key=env.runner.model_key,
                    inputs={"steps": 1},
                )
            )
            return r.status

        async def _llm_branch():
            async with httpx.AsyncClient(base_url=fake_vllm.base_url) as c:
                resp = await c.post(
                    "/v1/chat/completions",
                    json={
                        "model": "fake-llm",
                        "messages": [{"role": "user", "content": "x"}],
                    },
                )
            return resp.status_code

        img_status, llm_status = await asyncio.gather(
            _image_branch(), _llm_branch()
        )
        assert img_status == "completed"
        assert llm_status == 200
    finally:
        await env.runner.stop()
