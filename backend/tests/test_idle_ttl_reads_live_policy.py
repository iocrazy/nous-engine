"""回归:改了 resident/ttl 又 reload registry 后,TTL 与 LRU 守卫必须按**新策略**判定。

2026-09-16 真机事故:`wemm_embedding_4b` 的 yaml 加了 `resident: true`、删了
`ttl_seconds`,`POST /api/v1/engines/reload` 也调了,UI 上 `resident` 确实显示 True ——
但 84 分钟后它被 `TTL expired: unloading wemm_embedding_4b` 卸掉了。

根因是**两个真相源**:
  * `ModelManager.load_model` 对已加载模型早返回(`if self.is_loaded(...): touch(); return`),
    **不刷新 `entry.spec`** —— 所以 live entry 一直停在首次加载时那份旧 spec
    (resident=False / ttl_seconds=3600)。registry.reload() 只换了 `registry.specs`。
  * TTL 与 LRU 守卫读 `entry.spec`,而 API(`engines.py` 的 `resident=cfg.get(...)`)
    读的是配置。于是 UI 说 True、守卫看到 False,配置一改二者就分叉。

所以判据不能是"entry.spec 怎么写",而是"**当前生效的策略**怎么写"。
直接驱动 check_idle_models / evict_lru,不起任何子进程。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.services.model_manager import ModelManager

MID = "wemm_embedding_4b"


def _mgr(*, entry_resident: bool, entry_ttl: int, registry_resident: bool,
         registry_ttl: int | None, last_used: float) -> ModelManager:
    """entry.spec 是**陈旧**的那份;registry 里是 reload 之后的**新**策略。"""
    mgr = ModelManager.__new__(ModelManager)  # 绕过 __init__(会拉 GPU 探测)
    mgr._models = {
        MID: SimpleNamespace(
            spec=SimpleNamespace(id=MID, resident=entry_resident, ttl_seconds=entry_ttl),
            last_used=last_used,
            adapter=SimpleNamespace(is_loaded=True),
            stashed=False,
            cards=lambda: [1],
            gpu_index=1,
        )
    }
    mgr._references = {MID: set()}
    mgr._in_use = set()
    mgr.unload_model = AsyncMock(return_value=True)
    mgr.stash_model = AsyncMock(return_value=False)
    mgr._registry = SimpleNamespace(
        get=lambda mid: SimpleNamespace(
            id=mid, resident=registry_resident, ttl_seconds=registry_ttl,
        ) if mid == MID else None
    )
    return mgr


@pytest.mark.asyncio
async def test_ttl_respects_resident_flipped_on_after_reload(monkeypatch):
    """entry.spec 还是旧的(非常驻 + ttl 3600),但 registry 已改成常驻 → 不该回收。"""
    import time
    monkeypatch.setattr(time, "monotonic", lambda: 10_000.0)
    mgr = _mgr(entry_resident=False, entry_ttl=3600, registry_resident=True,
               registry_ttl=None, last_used=10_000.0 - 3601)
    await mgr.check_idle_models()
    mgr.unload_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_ttl_still_reclaims_when_policy_says_non_resident(monkeypatch):
    """反向:registry 说非常驻就照常回收 —— 别把守卫改成永不回收。"""
    import time
    monkeypatch.setattr(time, "monotonic", lambda: 10_000.0)
    mgr = _mgr(entry_resident=False, entry_ttl=300, registry_resident=False,
               registry_ttl=300, last_used=10_000.0 - 301)
    await mgr.check_idle_models()
    mgr.unload_model.assert_awaited_once_with(MID)


@pytest.mark.asyncio
async def test_evict_lru_skips_model_made_resident_after_reload():
    """LRU 驱逐同样要按新策略:reload 后已是常驻的模型不能被显存守卫驱逐。"""
    mgr = _mgr(entry_resident=False, entry_ttl=3600, registry_resident=True,
               registry_ttl=None, last_used=0.0)
    assert await mgr.evict_lru(gpu_index=1) is None
    mgr.unload_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_ui_toggle_off_still_reclaims(monkeypatch):
    """UI 关掉常驻后必须仍能回收 —— 防止"改成读 registry"把关闭操作吞掉。

    `PATCH /engines/{name}/resident` 走 `set_model_resident`,它**同时**更新
    registry spec 与 live entry(model_manager.py:914-920)。这里模拟那条路径:
    两边都变 False 之后,TTL 该照常回收。
    """
    import time
    monkeypatch.setattr(time, "monotonic", lambda: 10_000.0)
    mgr = _mgr(entry_resident=True, entry_ttl=300, registry_resident=True,
               registry_ttl=300, last_used=10_000.0 - 301)
    # set_model_resident(False) 的效果:registry 与 entry 同时翻成 False
    mgr._registry = SimpleNamespace(
        get=lambda mid: SimpleNamespace(id=mid, resident=False, ttl_seconds=300))
    mgr._models[MID].spec = SimpleNamespace(id=MID, resident=False, ttl_seconds=300)
    await mgr.check_idle_models()
    mgr.unload_model.assert_awaited_once_with(MID)


@pytest.mark.asyncio
async def test_falls_back_to_entry_spec_when_not_in_registry(monkeypatch):
    """registry 里没有(测试注入的 adapter_factory 模型、组件等)→ 退回 entry.spec。"""
    import time
    monkeypatch.setattr(time, "monotonic", lambda: 10_000.0)
    mgr = _mgr(entry_resident=True, entry_ttl=300, registry_resident=False,
               registry_ttl=300, last_used=10_000.0 - 301)
    mgr._registry = SimpleNamespace(get=lambda mid: None)  # registry 查不到
    await mgr.check_idle_models()
    mgr.unload_model.assert_not_awaited()  # entry.spec.resident=True 生效
