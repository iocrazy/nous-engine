"""Lane J: fake_runner fixture self-test — real subprocess running Lane C runner_main + FakeAdapter."""
import asyncio

import pytest

from src.runner import protocol as P

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_fake_runner_handshakes_and_runs_node(fake_runner):
    """fake_runner spawns an image runner that can load_model + run_node → completed."""
    runner = fake_runner(group_id="image", gpus=[2])
    await runner.start()
    try:
        loaded = await runner.client.load_model(runner.model_key, config={})
        assert loaded is True
        result = await runner.client.run_node(
            P.RunNode(
                task_id=1,
                node_id="sampler",
                node_type="tts",
                model_key=runner.model_key,
                inputs={"steps": 1},
            )
        )
        assert result.status == "completed"
        assert result.task_id == 1
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_fake_runner_crash_on_node_yields_failed_result(fake_runner):
    """crash_on_node=True → FakeAdapter raises RuntimeError → NodeResult(failed).

    FakeAdapter.infer raises RuntimeError (NOT a real process crash) so the
    runner catches it and emits NodeResult(status='failed'). The crash-detection
    chain (pipe EOF / watchdog) is exercised separately by test_runner_resilience.
    """
    runner = fake_runner(group_id="image", gpus=[2], crash_on_node=True)
    await runner.start()
    try:
        loaded = await runner.client.load_model(runner.model_key, config={})
        assert loaded is True
        result = await asyncio.wait_for(
            runner.client.run_node(
                P.RunNode(
                    task_id=2,
                    node_id="n",
                    node_type="tts",
                    model_key=runner.model_key,
                    inputs={"steps": 1},
                )
            ),
            timeout=10.0,
        )
        assert result.status == "failed"
        assert result.error and "fake adapter crash" in result.error
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_fake_runner_fail_load(fake_runner):
    """fail_load=True → load_model() returns False (FakeLoadError mapped to load_failed)."""
    runner = fake_runner(group_id="image", gpus=[2], fail_load=True)
    await runner.start()
    try:
        loaded = await runner.client.load_model(runner.model_key, config={})
        assert loaded is False
    finally:
        await runner.stop()
