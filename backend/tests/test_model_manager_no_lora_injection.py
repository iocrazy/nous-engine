"""_instantiate_adapter 不给非图像 adapter 塞 lora_paths(TTS/LLM 收到未知 kwarg 会崩)。

从 test_lora_scanner.py 挪来(2026-09-26 自建图像引擎删除,Task 4):lora 扫描与图像 spec
注入随图像引擎删掉,这条非图像断言逐字保留。
"""
from __future__ import annotations

from unittest.mock import MagicMock


def test_instantiate_adapter_skips_injection_for_non_image():
    """A TTS/LLM adapter would crash if we passed an unknown kwarg."""
    from src.services.inference.base import InferenceAdapter, MediaModality
    from src.services.inference.registry import ModelSpec
    from src.services.model_manager import ModelManager

    captured: dict = {}

    class FakeTTSAdapter(InferenceAdapter):
        modality = MediaModality.AUDIO
        estimated_vram_mb = 1

        def __init__(self, paths, **kwargs):
            super().__init__(paths=paths)
            captured["kwargs"] = dict(kwargs)

        async def load(self, device): self._model = True
        async def infer(self, req): ...

    import sys
    fake_mod = type(sys)("fake_tts_mod_3")
    fake_mod.FakeTTSAdapter = FakeTTSAdapter
    sys.modules["fake_tts_mod_3"] = fake_mod

    spec = ModelSpec(
        id="fake-tts",
        model_type="tts",
        adapter_class="fake_tts_mod_3.FakeTTSAdapter",
        paths={"main": "/x"},
        vram_mb=1,
    )
    registry = MagicMock()
    registry.get = lambda mid: spec if mid == spec.id else None
    allocator = MagicMock()
    mgr = ModelManager(registry=registry, allocator=allocator)

    mgr._instantiate_adapter(spec)
    assert "lora_paths" not in captured["kwargs"]
