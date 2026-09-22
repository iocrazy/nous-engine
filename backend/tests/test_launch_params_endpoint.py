"""PATCH /engines/{name}/launch-params —— 启动参数运行时覆盖。

用 `db_client` fixture(需要真 DB 写 override);**不传 admin headers** ——
conftest.py:84 强制 `ADMIN_PASSWORD=""`,整个测试套件里 admin 闸门是关的。

2026-09-22 之前这个端点是**坏的**:它写 configs/models.yaml,而模型定义 2026-06-20
已迁到 models.d/,那文件只剩 `models: []` 空锚点 → 遍历空列表 → 对**任何**模型都 404。
零测试覆盖,所以坏了没人发现。本文件就是补这个网。
"""
import pytest


@pytest.mark.asyncio
async def test_patch_launch_params_persists(db_client):
    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={"max_model_len": 16384})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["params"]["max_model_len"] == 16384
    assert body["applied"] is False   # 参数只在下次 load 时读

    from src.services import runtime_override_store
    assert runtime_override_store.get_overrides()[
        "qwen3_8_27b_abliterated_awq"]["params"]["max_model_len"] == 16384


@pytest.mark.asyncio
async def test_patch_rejects_gpu_memory_utilization(db_client):
    """util 是「占该卡总量」的比例,换卡必须重算 —— 把它做成按钮就是把坑做成按钮。
    显存要改走 vram-budget(绝对 GiB,加载时按实际那张卡换算)。"""
    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={"gpu_memory_utilization": 0.9})
    assert r.status_code == 400
    assert "vram-budget" in r.text


@pytest.mark.asyncio
async def test_patch_rejects_tensor_parallel_size(db_client):
    """tp 是**放置结论**(_placement 定),不是调优旋钮。"""
    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={"tensor_parallel_size": 2})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_patch_rejects_unknown_key(db_client):
    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={"nonsense_flag": 1})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_patch_unknown_engine_404(db_client):
    r = await db_client.patch(
        "/api/v1/engines/no_such_model/launch-params",
        json={"max_model_len": 4096})
    assert r.status_code == 404


def test_whitelist_excludes_placement_and_vram_knobs():
    """白名单绝不能混进放置/显存参数 —— 它们是放置结论,不是调优旋钮。
    靠注释守不住:将来有人图省事往白名单里加一个,这条会红。

    尤其要紧的是 `tensor_parallel_size`:`_resolve_placement` 会读
    `spec.params["tensor_parallel_size"]`,而适配器构造参数现在真的会被 params 覆盖
    叠加(2026-09-22 的 C1 修复)—— 白名单一旦放行,放置结论就能从 UI 上被改。
    """
    from src.api.routes.engines import _LAUNCH_PARAM_WHITELIST

    forbidden = {"gpu", "gpus", "device", "tensor_parallel_size", "gpu_memory_utilization",
                 "vram_budget", "vram_mb", "kv_cache_dtype"}
    assert not (_LAUNCH_PARAM_WHITELIST & forbidden)


@pytest.mark.asyncio
async def test_patch_null_clears_single_key(db_client):
    name = "qwen3_8_27b_abliterated_awq"
    await db_client.patch(f"/api/v1/engines/{name}/launch-params",
                       json={"max_model_len": 16384, "max_num_seqs": 4})
    r = await db_client.patch(f"/api/v1/engines/{name}/launch-params",
                              json={"max_num_seqs": None})
    assert r.status_code == 200
    assert r.json()["params"] == {"max_model_len": 16384}
