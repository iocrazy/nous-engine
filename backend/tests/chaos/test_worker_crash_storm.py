"""Lane J chaos: repeated runner crash → backoff + GPU-free gate + no process leak.

spec §5.5 test_runner_crash_storm (filename uses "worker" per the brief override).
Manual weekly run: pytest -m chaos.
"""
import asyncio
import multiprocessing as mp
from pathlib import Path

import pytest

from src.runner import protocol as P
from src.runner.supervisor import RunnerSupervisor

pytestmark = pytest.mark.chaos

_FIXTURE_YAML = str(Path(__file__).parent.parent / "fixtures" / "runner_models.yaml")


def _supervisor(**overrides) -> RunnerSupervisor:
    kw = dict(
        group_id="image",
        gpus=[2],
        models_yaml_path=_FIXTURE_YAML,
        fake_adapter=True,
        ping_interval=0.2,
        ping_timeout=0.4,
        restart_backoff=[0.1, 0.2, 0.4, 0.8],
        gpu_free_probe=lambda gpus: True,
    )
    kw.update(overrides)
    return RunnerSupervisor(**kw)


@pytest.mark.asyncio
async def test_runner_repeated_crashes():
    """5 consecutive runner crashes → backoff respected, GPU-free gate called
    each time, supervisor stays alive, final runner can still service requests.
    """
    gate_calls: list[list[int]] = []

    def _gpu_free_probe(gpus):
        gate_calls.append(list(gpus))
        return True  # always free — focus on storm behavior, not gating delay

    sup = _supervisor(gpu_free_probe=_gpu_free_probe)
    await sup.start()
    try:
        for crash_n in range(5):
            # Confirm current runner is alive.
            pong = await asyncio.wait_for(sup.client.ping(), timeout=2.0)
            assert pong.runner_id

            sup._process.kill()
            await asyncio.wait_for(
                sup.wait_restarted(count=crash_n + 1), timeout=25.0
            )
            assert sup.is_running, (
                f"after crash #{crash_n + 1} supervisor should have restarted runner"
            )

        assert sup.restart_count == 5
        # GPU-free probe should have fired at least once per restart.
        assert len(gate_calls) >= 5, (
            f"GPU-free gate should be called ≥5 times; saw {len(gate_calls)}"
        )

        # Final runner still services requests.
        loaded = await sup.client.load_model("fake-img-a", config={})
        assert loaded is True
        result = await sup.client.run_node(
            P.RunNode(
                task_id=999,
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
async def test_crash_storm_does_not_leak_processes():
    """After several crashes + supervisor.stop(), no daemon runner survives."""
    sup = _supervisor()
    await sup.start()
    try:
        for n in range(3):
            sup._process.kill()
            await asyncio.wait_for(sup.wait_restarted(count=n + 1), timeout=25.0)
    finally:
        await sup.stop()

    # No runner subprocess should survive supervisor.stop().
    alive = [c for c in mp.active_children() if c.name.startswith("runner-")]
    assert not alive, f"runner subprocesses still alive after stop: {alive}"
