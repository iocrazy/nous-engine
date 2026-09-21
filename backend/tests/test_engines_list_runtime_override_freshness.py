"""引擎列表必须**立刻**反映运行时覆盖(resident / gpu / gpus / vram_budget)。

线上 bug(2026-09-03 复现):引擎库右键「取消自动加载」→
`PATCH /api/v1/engines/{name}/resident?resident=false` 返回 200、DB 与
runtime_override_store 内存缓存都已更新,可前端重拉 `GET /api/v1/engines` 拿到
**304 Not Modified**,📌 常驻徽标纹丝不动,用户以为没生效又点一次。

根因:`scan_models()` 的 30s TTL 缓存把「磁盘扫描 + yaml + 运行时覆盖」**一起**缓存了
(`_scan_models_uncached` 里 `load_model_configs()` 末尾就叠了覆盖)。写路径只
`invalidate("engines")` 清 HTTP 响应缓存,重算的 body 仍是 30s 前的旧覆盖值 →
字节相同 → ETag 相同 → 304。

修法(分层):扫描缓存只存不含覆盖的基础结果,`scan_models()` 返回前每次从
`runtime_override_store`(进程内 write-through 缓存,零 I/O)现叠。本文件钉死这条。
"""
from unittest.mock import patch

import pytest

from src.api.response_cache import invalidate
from src.services.model_scanner import invalidate_scan_cache

ENGINE = "qwen3_embedding_4b"
LOCAL_PATH = "embedding/Qwen3-Embedding-4B"


@pytest.fixture(autouse=True)
def _fresh_caches():
    """两层缓存都是进程级的,用例之间必须清 —— 否则上一条的 body/ETag 漏给下一条。"""
    invalidate("engines")
    invalidate_scan_cache()
    yield
    invalidate("engines")
    invalidate_scan_cache()


async def _get_engines(client, headers=None):
    with patch("src.api.routes.engines.scan_local_models", return_value={LOCAL_PATH}):
        return await client.get("/api/v1/engines", headers=headers or {})


def _pick(resp, name=ENGINE):
    for e in resp.json():
        if e["name"] == name:
            return e
    raise AssertionError(f"{name} 不在 /api/v1/engines 返回里:{[e['name'] for e in resp.json()]}")


# ---------------------------------------------------------------------------
# resident —— 线上那条 bug 本体
# ---------------------------------------------------------------------------

async def test_patch_resident_visible_in_next_list_get(db_client):
    """PATCH resident=true → 下一次 GET 立刻 True 且 ETag 变;再 false → 立刻变回。"""
    first = await _get_engines(db_client)
    assert first.status_code == 200
    assert _pick(first)["resident"] is False
    etag0 = first.headers["ETag"]

    r = await db_client.patch(f"/api/v1/engines/{ENGINE}/resident?resident=true")
    assert r.status_code == 200, r.text

    second = await _get_engines(db_client)
    assert _pick(second)["resident"] is True, "覆盖被扫描缓存吞了(原 bug)"
    etag1 = second.headers["ETag"]
    assert etag1 != etag0, "body 变了 ETag 却没变 → 浏览器会一直吃 304"

    r = await db_client.patch(f"/api/v1/engines/{ENGINE}/resident?resident=false")
    assert r.status_code == 200, r.text

    third = await _get_engines(db_client)
    assert _pick(third)["resident"] is False
    assert third.headers["ETag"] != etag1


async def test_patch_resident_breaks_conditional_304(db_client):
    """带旧 If-None-Match 重拉:改了覆盖就必须给 200 + 新 body,不能再回 304。"""
    first = await _get_engines(db_client)
    etag0 = first.headers["ETag"]

    # 没有写入时,条件请求照旧 304(缓存本身仍然有效,别把它一起改坏了)。
    unchanged = await _get_engines(db_client, headers={"If-None-Match": etag0})
    assert unchanged.status_code == 304

    await db_client.patch(f"/api/v1/engines/{ENGINE}/resident?resident=true")

    after = await _get_engines(db_client, headers={"If-None-Match": etag0})
    assert after.status_code == 200, "写完覆盖还回 304 —— 就是用户看到的那个 bug"
    assert _pick(after)["resident"] is True


# ---------------------------------------------------------------------------
# gpu / gpus / vram_budget —— 同一条修法覆盖的其余写路径
# ---------------------------------------------------------------------------

async def test_patch_gpu_single_card_visible_in_next_list_get(db_client):
    """`?gpu=N` 单卡钉卡同样即时可见 + ETag 变。"""
    first = await _get_engines(db_client)
    before = _pick(first)
    etag0 = first.headers["ETag"]
    target = 2 if before["gpu"] != 2 else 1

    r = await db_client.patch(f"/api/v1/engines/{ENGINE}/gpu?gpu={target}")
    assert r.status_code == 200, r.text

    second = await _get_engines(db_client)
    after = _pick(second)
    assert after["gpu"] == target
    assert after["gpus"] is None
    assert second.headers["ETag"] != etag0


async def test_patch_gpu_group_visible_in_next_list_get(db_client):
    """`{"gpus": [...]}` 组同样即时可见。校验路径全 mock,绝不碰真 GPU。"""
    from src.gpu import topology

    names = {0: "NVIDIA GeForce RTX 3090", 1: "NVIDIA RTX PRO 6000",
             2: "NVIDIA GeForce RTX 3090"}
    with patch.object(topology, "gpu_names", return_value=names), \
            patch.object(topology, "gpu_totals_gb", return_value={0: 24.0, 1: 96.0, 2: 24.0}), \
            patch.object(topology, "display_gpu_indices", return_value=set()), \
            patch.object(topology, "nvlink_pairs", return_value={frozenset((0, 2))}):
        first = await _get_engines(db_client)
        etag0 = first.headers["ETag"]
        assert _pick(first)["gpus"] is None

        r = await db_client.patch(f"/api/v1/engines/{ENGINE}/gpu", json={"gpus": [2, 0]})
        assert r.status_code == 200, r.text

        second = await _get_engines(db_client)
        after = _pick(second)

    assert after["gpus"] == [0, 2]
    assert after["gpu"] == 0
    assert second.headers["ETag"] != etag0


async def test_patch_vram_budget_visible_in_next_scan(db_client):
    """vram_budget 不在列表 body 里,但配置读取(scan_models)必须立刻拿到新值 ——
    加载路径读的就是它。"""
    from src.services.model_scanner import scan_models

    assert scan_models()[ENGINE].get("vram_budget") is None

    r = await db_client.patch(f"/api/v1/engines/{ENGINE}/vram-budget",
                              json={"mode": "percent", "value": 0.25})
    assert r.status_code == 200, r.text

    assert scan_models()[ENGINE]["vram_budget"] == {"mode": "percent", "value": 0.25}


# ---------------------------------------------------------------------------
# 扫描缓存本身不能被这次分层削掉
# ---------------------------------------------------------------------------

def test_scan_cache_still_avoids_a_second_disk_walk(monkeypatch):
    """两次 scan_models() 之间只走一次盘,但覆盖每次现叠(且不渗进缓存)。"""
    from src.services import model_scanner as ms
    from src.services import runtime_override_store as store

    calls = {"n": 0}

    def _fake_scan():
        calls["n"] += 1
        return {ENGINE: {"name": ENGINE, "type": "embedding", "resident": False, "gpu": None}}

    monkeypatch.setattr(ms, "_scan_models_uncached", _fake_scan)
    ms.invalidate_scan_cache()
    store.set_cache_for_test({})

    assert ms.scan_models()[ENGINE]["resident"] is False
    assert calls["n"] == 1

    store.set_cache_for_test({ENGINE: {"resident": True, "gpu": 2}})
    hot = ms.scan_models()[ENGINE]
    assert calls["n"] == 1, "第二次读不该再走盘 —— TTL 缓存必须还在"
    assert hot["resident"] is True and hot["gpu"] == 2

    # 覆盖撤销后立刻退回基础值:说明叠加没有写穿进 _SCAN_CACHE。
    store.set_cache_for_test({})
    cold = ms.scan_models()[ENGINE]
    assert calls["n"] == 1
    assert cold["resident"] is False and cold["gpu"] is None


def test_scan_cache_holds_no_override_values(monkeypatch):
    """直白版:_SCAN_CACHE 里存的永远是干净的基础结果。"""
    from src.services import model_scanner as ms
    from src.services import runtime_override_store as store

    monkeypatch.setattr(
        ms, "_scan_models_uncached",
        lambda: {ENGINE: {"name": ENGINE, "type": "embedding", "resident": False}},
    )
    ms.invalidate_scan_cache()
    store.set_cache_for_test({ENGINE: {"resident": True}})

    assert ms.scan_models()[ENGINE]["resident"] is True
    assert ms._SCAN_CACHE["data"][ENGINE]["resident"] is False
