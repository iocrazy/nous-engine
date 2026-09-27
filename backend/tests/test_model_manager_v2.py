import pytest
from unittest.mock import MagicMock
from src.services.inference.base import (
    InferenceAdapter,
    InferenceResult,
    MediaModality,
    UsageMeter,
)
from src.services.inference.registry import ModelSpec
from src.services.model_manager import ModelLoadError, ModelManager, ModelNotFoundError


class FakeAdapter(InferenceAdapter):
    modality = MediaModality.AUDIO
    estimated_vram_mb = 2000

    async def load(self, device: str) -> None:
        self._model = True

    async def infer(self, req) -> InferenceResult:
        return InferenceResult(
            media_type="text/plain",
            data=b"ok",
            usage=UsageMeter(latency_ms=1),
        )


def _make_spec(model_id="test-model", vram_mb=2000):
    return ModelSpec(
        id=model_id,
        model_type="tts",
        adapter_class="fake",
        paths={"main": "/fake"},
        vram_mb=vram_mb,
    )


def _make_manager(specs=None):
    registry = MagicMock()
    registry.get = lambda mid: next((s for s in (specs or []) if s.id == mid), None)
    # ModelManager.load_model falls back to add_from_scan when get() misses,
    # so the test mock must explicitly return None for unknown ids — otherwise
    # MagicMock returns a truthy MagicMock instance and "Unknown model" never raises.
    registry.add_from_scan = MagicMock(return_value=None)
    registry.specs = specs or []
    allocator = MagicMock()
    allocator.get_best_gpu = MagicMock(return_value=0)
    allocator.get_free_mb = MagicMock(return_value=20000)
    return ModelManager(registry=registry, allocator=allocator)


async def test_load_and_unload():
    mgr = _make_manager([_make_spec()])
    await mgr.load_model("test-model", adapter_factory=lambda sp: FakeAdapter(paths=sp.paths))
    assert mgr.is_loaded("test-model")
    await mgr.unload_model("test-model")
    assert not mgr.is_loaded("test-model")


async def test_load_unknown_model():
    mgr = _make_manager([])
    with pytest.raises(ValueError, match="Unknown model"):
        await mgr.load_model("nonexistent")


async def test_add_remove_reference():
    mgr = _make_manager([_make_spec()])
    await mgr.load_model("test-model", adapter_factory=lambda sp: FakeAdapter(paths=sp.paths))
    mgr.add_reference("test-model", "wf-1")
    assert mgr.get_references("test-model") == {"wf-1"}
    mgr.remove_reference("test-model", "wf-1")
    assert mgr.get_references("test-model") == set()


async def test_unload_skips_referenced():
    mgr = _make_manager([_make_spec()])
    await mgr.load_model("test-model", adapter_factory=lambda sp: FakeAdapter(paths=sp.paths))
    mgr.add_reference("test-model", "wf-1")
    await mgr.unload_model("test-model")
    assert mgr.is_loaded("test-model")  # still loaded


async def test_evict_lru():
    mgr = _make_manager([_make_spec("model-a"), _make_spec("model-b")])
    await mgr.load_model("model-a", adapter_factory=lambda sp: FakeAdapter(paths=sp.paths))
    await mgr.load_model("model-b", adapter_factory=lambda sp: FakeAdapter(paths=sp.paths))
    evicted = await mgr.evict_lru(gpu_index=0)
    assert evicted == "model-a"
    assert not mgr.is_loaded("model-a")
    assert mgr.is_loaded("model-b")


async def test_get_adapter():
    mgr = _make_manager([_make_spec()])
    await mgr.load_model("test-model", adapter_factory=lambda sp: FakeAdapter(paths=sp.paths))
    adapter = mgr.get_adapter("test-model")
    assert adapter is not None and adapter.is_loaded


# ----- get_loaded_adapter (PR-0 v2 helper) -----


async def test_get_loaded_adapter_already_loaded_fast_path():
    mgr = _make_manager([_make_spec()])
    await mgr.load_model("test-model", adapter_factory=lambda sp: FakeAdapter(paths=sp.paths))
    # Note: get_loaded_adapter uses spec.adapter_class for lazy load, but
    # since fast-path returns early, that path isn't exercised here.
    adapter = await mgr.get_loaded_adapter("test-model")
    assert adapter.is_loaded


async def test_get_loaded_adapter_unknown_raises_not_found():
    """Spec doesn't exist (yaml + scan miss) → ModelNotFoundError → HTTP 404."""
    mgr = _make_manager([])
    with pytest.raises(ModelNotFoundError):
        await mgr.get_loaded_adapter("nonexistent")


async def test_get_loaded_adapter_prior_failure_raises_load_error():
    """If a prior load failed, subsequent get_loaded_adapter raises immediately
    without retrying — admin must call load_model explicitly to clear."""
    mgr = _make_manager([_make_spec()])
    mgr._load_failures["test-model"] = "OOM during load"
    with pytest.raises(ModelLoadError) as exc_info:
        await mgr.get_loaded_adapter("test-model")
    assert "OOM during load" in exc_info.value.message
    assert exc_info.value.code == "model_load_failed"


async def test_get_loaded_adapter_load_failure_recorded():
    """Load failure → ModelLoadError + record in _load_failures for next call."""
    mgr = _make_manager([_make_spec()])

    def failing_factory(sp):
        raise RuntimeError("CUDA OOM")

    with pytest.raises(ModelLoadError):
        # adapter_factory only applies to load_model directly; get_loaded_adapter
        # goes through _instantiate_adapter. We exercise the failure path by
        # using a spec whose adapter_class doesn't exist.
        bad_spec = ModelSpec(
            id="bad-model",
            model_type="tts",
            adapter_class="nonexistent.module.BadAdapter",
            paths={"main": "/fake"},
            vram_mb=100,
        )
        mgr._registry.get = lambda mid: bad_spec if mid == "bad-model" else None
        mgr._registry.add_from_scan = MagicMock(return_value=None)
        await mgr.get_loaded_adapter("bad-model")

    # Failure recorded — second call raises immediately
    assert "bad-model" in mgr._load_failures
    with pytest.raises(ModelLoadError):
        await mgr.get_loaded_adapter("bad-model")


async def test_get_loaded_adapter_clears_failure_on_successful_reload():
    """After a failure, a successful explicit load_model clears the record."""
    mgr = _make_manager([_make_spec()])
    mgr._load_failures["test-model"] = "transient"
    await mgr.load_model("test-model", adapter_factory=lambda sp: FakeAdapter(paths=sp.paths))
    assert "test-model" not in mgr._load_failures
    adapter = await mgr.get_loaded_adapter("test-model")
    assert adapter.is_loaded


async def test_non_image_allocator_minus1_keeps_minus1():
    """LLM/TTS adapters honestly need full VRAM — leave -1 as a signal that
    something is wrong (no fallback to GPU 0)."""
    import sys
    from unittest.mock import MagicMock
    from src.services.inference.base import InferenceAdapter, MediaModality
    from src.services.inference.registry import ModelSpec
    from src.services.model_manager import ModelManager

    class FakeTTSAdapter(InferenceAdapter):
        modality = MediaModality.AUDIO
        estimated_vram_mb = 8000

        def __init__(self, paths, **kwargs):
            super().__init__(paths=paths)

        async def load(self, device): self._model = True
        async def infer(self, req): ...

    fake_mod = type(sys)("fake_tts_alloc_mod")
    fake_mod.FakeTTSAdapter = FakeTTSAdapter
    sys.modules["fake_tts_alloc_mod"] = fake_mod

    spec = ModelSpec(
        id="full-vram-tts",
        model_type="tts",
        adapter_class="fake_tts_alloc_mod.FakeTTSAdapter",
        paths={"main": "/x"},
        vram_mb=8000,
    )
    registry = MagicMock()
    registry.get = lambda mid: spec if mid == spec.id else None
    registry.add_from_scan = MagicMock(return_value=None)
    allocator = MagicMock()
    allocator.get_best_gpu = MagicMock(return_value=-1)

    mgr = ModelManager(registry=registry, allocator=allocator)
    await mgr.load_model("full-vram-tts")

    lm = mgr._models["full-vram-tts"]
    # TTS keeps -1 as honest signal (no fallback)
    assert lm.gpu_index == -1
    assert lm.gpu_indices == []
