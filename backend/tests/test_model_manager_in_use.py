"""in-use 硬守卫:正在 infer 的 adapter 不被 unload(即使 force)/ evict_lru 驱逐。

从 test_get_or_load_image_adapter.py 挪来(2026-09-26 自建图像引擎删除,Task 4):原用例经
图像 adapter 路径装一个模型;守卫本身是 LLM/TTS 共用的,这里直接放一个 LoadedModel,
断言逐字不变。
"""
from __future__ import annotations

import pytest

from src.services.gpu_allocator import GPUAllocator
from src.services.inference.base import InferenceAdapter
from src.services.inference.registry import ModelRegistry, ModelSpec
from src.services.model_manager import LoadedModel, ModelManager


class _EmptyRegistry(ModelRegistry):
    def __init__(self):
        self._config_path = ""
        self._specs = {}


class _FakeAdapter(InferenceAdapter):
    modality = None
    estimated_vram_mb = 0

    def __init__(self):
        self._model = object()

    async def load(self, device):  # pragma: no cover
        pass

    async def infer(self, req):  # pragma: no cover
        raise NotImplementedError

    def unload(self):
        self._model = None

    def stash(self):
        return False  # 走销毁路径,与原用例的图像 adapter 一致


@pytest.fixture
def loaded():
    mm = ModelManager(registry=_EmptyRegistry(), allocator=GPUAllocator())
    adapter = _FakeAdapter()
    spec = ModelSpec(id="tts:fake", model_type="tts", adapter_class="fake",
                     paths={"main": "/m/x"}, vram_mb=1000)
    mm._models["tts:fake"] = LoadedModel(spec=spec, adapter=adapter, gpu_index=1)
    return mm, adapter


@pytest.mark.asyncio
async def test_in_use_adapter_not_unloaded_even_force(loaded):
    """in-use 守卫:正在 infer 的 adapter,unload 即使 force=True 也拒绝(否则 mid-CUDA 卸载 segfault)。
    释放后才可卸载。"""
    mm, adapter = loaded
    mid = next(iter(mm._models))

    mm.mark_adapter_in_use(adapter)
    assert mid in mm._in_use
    await mm.unload_model(mid, force=True)
    assert mid in mm._models  # in-use → 拒绝卸载

    mm.release_adapter(adapter)
    assert mid not in mm._in_use
    await mm.unload_model(mid, force=True)
    assert mid not in mm._models  # 释放后可卸载


@pytest.mark.asyncio
async def test_evict_lru_skips_in_use(loaded):
    """evict_lru 不驱逐正在 infer 的 adapter(否则正用的被踢 → segfault / 重载)。"""
    mm, adapter = loaded
    mid = next(iter(mm._models))
    mm.mark_adapter_in_use(adapter)
    assert await mm.evict_lru() is None  # 唯一候选在 in-use → 不驱逐
    assert mid in mm._models
