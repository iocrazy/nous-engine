"""adapter 级 RAM stash(spec 2026-06-12 PR-2):整模型 stash + 守卫 + 记账 + LRU 驱逐优先 stash。

从 test_adapter_ram_stash.py 挪来(2026-09-26 自建图像引擎删除,Task 4):那个文件里的
组件池 / ModularImageBackend 用例随图像引擎删掉,adapter 级 `stash_model` 是 LLM/TTS
路径共用的,用例原样保留(断言逐字不变;夹具里的模型 id / 类型改成中性值)。
"""
from __future__ import annotations

from types import SimpleNamespace

import psutil
import pytest

from src.services.gpu_allocator import GPUAllocator
from src.services.inference.base import InferenceAdapter
from src.services.inference.registry import ModelRegistry, ModelSpec
from src.services.model_manager import LoadedModel, ModelManager


class _Reg(ModelRegistry):
    def __init__(self):
        self._config_path = ""
        self._specs = {}


class _FakeAdapter(InferenceAdapter):
    modality = None
    estimated_vram_mb = 0

    def __init__(self, stash_ok=True):
        self._stash_ok = stash_ok
        self.stash_calls = 0
        self.restore_calls = 0
        self._model = object()

    async def load(self, device):  # pragma: no cover
        pass

    async def infer(self, req):  # pragma: no cover
        raise NotImplementedError

    def stash(self):
        self.stash_calls += 1
        return self._stash_ok

    def restore(self):
        self.restore_calls += 1


def _spec(mid="tts:fake:abc", vram=1000, resident=False):
    return ModelSpec(id=mid, model_type="tts", adapter_class="fake",
                     paths={"main": "/m/x"}, vram_mb=vram, resident=resident)


def _entry(mm, adapter=None, mid="tts:fake:abc", **kw):
    a = adapter or _FakeAdapter()
    e = LoadedModel(spec=_spec(mid, **kw), adapter=a, gpu_index=1)
    mm._models[mid] = e
    return e, a


@pytest.fixture
def mm(monkeypatch):
    m = ModelManager(registry=_Reg(), allocator=GPUAllocator())
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=100 * 10**9))
    return m


@pytest.mark.asyncio
async def test_stash_model_marks_and_calls_adapter(mm):
    e, a = _entry(mm)
    assert await mm.stash_model(e.spec.id) is True
    assert e.stashed is True and a.stash_calls == 1


@pytest.mark.asyncio
async def test_stash_model_guards(mm):
    """in_use / resident / 被引用 / 已 stashed(二次=真销毁)/ 引擎不支持 → False。"""
    e, a = _entry(mm, mid="m1")
    mm._in_use["m1"] = 1  # C5:_in_use 现为引用计数 dict(原 set)
    assert await mm.stash_model("m1") is False
    mm._in_use.pop("m1", None)

    e2, _ = _entry(mm, mid="m2", resident=True)
    assert await mm.stash_model("m2") is False

    e3, _ = _entry(mm, mid="m3")
    mm._references["m3"] = {"someone"}
    assert await mm.stash_model("m3") is False

    e4, _ = _entry(mm, mid="m4")
    assert await mm.stash_model("m4") is True
    assert await mm.stash_model("m4") is False, "已 stashed 再卸 = 真销毁路径"

    e5, _ = _entry(mm, adapter=_FakeAdapter(stash_ok=False), mid="m5")
    assert await mm.stash_model("m5") is False


@pytest.mark.asyncio
async def test_stash_model_low_ram_refuses(mm, monkeypatch):
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: SimpleNamespace(available=1 * 10**9))
    e, a = _entry(mm)
    assert await mm.stash_model(e.spec.id) is False


def test_snapshot_reports_adapter_stashed(mm):
    e, _ = _entry(mm)
    e.stashed = True
    snap = mm.loaded_models_snapshot()
    assert snap and snap[0]["stashed"] is True


@pytest.mark.asyncio
async def test_evict_lru_stashes_first(mm):
    """守卫驱逐优先 stash(PR-3):可 stash 的 entry → evict 返回 id 且 entry 留存(stashed)。"""
    e, a = _entry(mm, mid="mA")
    out = await mm.evict_lru(gpu_index=1)
    assert out == "mA"
    assert mm._models["mA"].stashed is True and a.stash_calls == 1


@pytest.mark.asyncio
async def test_evict_lru_skips_stashed_candidates(mm):
    """stashed entry 不占卡 → 不是驱逐候选(选它销毁腾不出显存,守卫空转)。"""
    e, _ = _entry(mm, mid="mB")
    e.stashed = True
    assert await mm.evict_lru(gpu_index=1) is None


@pytest.mark.asyncio
async def test_evict_lru_falls_back_to_destroy(mm):
    """不可 stash(引擎不支持)→ 旧销毁路径,entry 出 _models。"""
    e, a = _entry(mm, adapter=_FakeAdapter(stash_ok=False), mid="mC")
    out = await mm.evict_lru(gpu_index=1)
    assert out == "mC" and "mC" not in mm._models


# 从 test_component_ram_stash.py 挪来:组件池那一半随图像引擎删掉,adapter 级记账留着
# (/monitor/stats 聚合用)。
def test_stash_ram_bytes_sums_adapters():
    """stash_ram_bytes = stashed adapter 的 vram_mb 字节之和(/monitor/stats 聚合用)。未 stash 的不计。"""
    mm = ModelManager(registry=_Reg(), allocator=GPUAllocator())
    # adapter:一个 stashed(spec.vram_mb=3000MB)、一个未 stash(不计)
    mm._models = {
        "m1": SimpleNamespace(stashed=True, spec=SimpleNamespace(vram_mb=3000)),
        "m2": SimpleNamespace(stashed=False, spec=SimpleNamespace(vram_mb=8000)),
    }
    expected = 3000 * 1024 * 1024
    assert mm.stash_ram_bytes() == expected


def test_stash_ram_bytes_empty_is_zero():
    """无 stash → 0(不抛)。"""
    mm = ModelManager(registry=_Reg(), allocator=GPUAllocator())
    assert mm.stash_ram_bytes() == 0
