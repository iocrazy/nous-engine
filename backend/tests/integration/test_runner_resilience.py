"""Lane J integration: runner crash detection / restart preload / load_failed
non-blocking / LLM-not-serialized / main-process Abort view (spec §5.3 resilience).

Complementary to Lane C tests/test_runner_supervisor.py (subprocess-internal
view) and tests/test_runner_process.py (pipe-reader/executor split). Lane J's
version asserts what the main process observes through RunnerClient.
"""
import asyncio
from pathlib import Path

import pytest

from src.runner import protocol as P
from src.runner.supervisor import RunnerSupervisor

pytestmark = pytest.mark.integration

_FIXTURE_YAML = str(Path(__file__).parent.parent / "fixtures" / "runner_models.yaml")


def _supervisor(**overrides) -> RunnerSupervisor:
    """Supervisor wired for fast tests: short ping/backoff + always-free GPU."""
    kw = dict(
        group_id="image",
        gpus=[2],
        models_yaml_path=_FIXTURE_YAML,
        fake_adapter=True,
        ping_interval=0.3,
        ping_timeout=0.5,
        restart_backoff=[0.1, 0.2],
        gpu_free_probe=lambda gpus: True,
    )
    kw.update(overrides)
    return RunnerSupervisor(**kw)


@pytest.mark.asyncio
async def test_runner_crash_detected_inflight_marked_failed():
    """kill runner → watchdog detects → inflight task callback fires with reason."""
    failed: list[tuple[int, str]] = []
    sup = _supervisor(on_task_failed=lambda tid, reason: failed.append((tid, reason)))
    await sup.start()
    try:
        # Register a fake inflight task, then hard-kill the runner — supervisor
        # should fire on_task_failed with reason "runner_crashed".
        sup.register_inflight(999)
        old_pid = sup.pid
        sup._process.kill()
        await asyncio.wait_for(sup.wait_restarted(count=1), timeout=15.0)

        assert sup.is_running
        assert sup.pid != old_pid
        assert sup.restart_count == 1
        assert (999, "runner_crashed") in failed
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_runner_restart_can_load_and_run_again():
    """After crash + restart, runner still services load_model + run_node.

    Asserts the "restart re-preload" pathway is not broken — preload itself
    is wired by Lane H (resident models), but the load_model → run_node
    capability post-restart is what Lane J integration covers.
    """
    sup = _supervisor()
    await sup.start()
    try:
        sup._process.kill()
        await asyncio.wait_for(sup.wait_restarted(count=1), timeout=15.0)
        # New runner accepts load + run_node.
        loaded = await sup.client.load_model("fake-img-a", config={})
        assert loaded is True
        result = await sup.client.run_node(
            P.RunNode(
                task_id=1,
                node_id="n",
                node_type="tts",
                model_key="fake-img-a",
                inputs={},
            )
        )
        assert result.status == "completed"
    finally:
        await sup.stop()


@pytest.mark.asyncio
async def test_model_load_failed_non_blocking(fake_runner):
    """Model load_failed → load_model returns False; runner still responds (ping)."""
    runner = fake_runner(group_id="image", gpus=[2], fail_load=True)
    await runner.start()
    try:
        loaded = await runner.client.load_model(runner.model_key, config={})
        assert loaded is False

        pong = await asyncio.wait_for(runner.client.ping(), timeout=3.0)
        assert pong.runner_id
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_llm_runner_does_not_serialize(fake_vllm):
    """LLM direct path: concurrent requests overlap (not serialized) — spec §5.3."""
    import httpx

    async def _one():
        async with httpx.AsyncClient(base_url=fake_vllm.base_url) as c:
            r = await c.post(
                "/v1/chat/completions",
                json={
                    "model": "fake-llm",
                    "messages": [{"role": "user", "content": "x"}],
                },
            )
            return r.status_code

    statuses = await asyncio.gather(*[_one() for _ in range(5)])
    assert all(s == 200 for s in statuses)
    assert fake_vllm.max_concurrent_seen >= 2, (
        "concurrent LLM requests must overlap; observed max=%d"
        % fake_vllm.max_concurrent_seen
    )


@pytest.mark.asyncio
async def test_abort_during_node_execution_main_process_view(fake_runner):
    """Main-process view: run_node mid-flight + Abort → cancelled NodeResult.

    Complementary to Lane C test_runner_process.py (subprocess-internal view
    of pipe-reader / executor split). Lane J asserts only what RunnerClient sees.
    """
    runner = fake_runner(group_id="image", gpus=[2], slow_seconds=0.15)
    await runner.start()
    try:
        # steps 经 LoadModel.config 给 FakeAdapter(AudioRequest 不带 steps)。
        assert await runner.client.load_model(runner.model_key, config={"steps": 30}) is True
        coro = asyncio.create_task(
            runner.client.run_node(
                P.RunNode(
                    task_id=9,
                    node_id="n",
                    node_type="tts",
                    model_key=runner.model_key,
                    inputs={},  # config steps=30 × slow 0.15s = 4.5s budget for cancel
                )
            )
        )
        await asyncio.sleep(0.2)
        await runner.client.abort(task_id=9, node_id="n")
        result = await asyncio.wait_for(coro, timeout=10.0)
        assert result.status == "cancelled"
    finally:
        await runner.stop()
