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


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["max_model_len", "max_num_seqs", "max_num_batched_tokens"])
@pytest.mark.parametrize("value", [0, -1, "8192", 1.5, True])
async def test_patch_rejects_bad_numeric_values(db_client, key, value):
    """只校验键名不校验值是不够的:前端 `Number('')` 是 `0`,清空输入框就会把 0 写进库,
    而 0 让 vLLM 起不来 —— 库里躺着一个起不来的值,还得人去 DB 里捞。

    `True` 单独要紧:`bool` 是 `int` 的子类,不显式排除会被当成 1 存下去。
    """
    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={key: value})
    assert r.status_code == 400, r.text
    assert key in r.text          # 报错要点名是哪个键


@pytest.mark.asyncio
async def test_patch_rejects_non_bool_prefix_caching(db_client):
    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={"enable_prefix_caching": 1})
    assert r.status_code == 400
    assert "enable_prefix_caching" in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "   ", 8, True])
async def test_patch_rejects_bad_dtype(db_client, value):
    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={"dtype": value})
    assert r.status_code == 400
    assert "dtype" in r.text


@pytest.mark.asyncio
async def test_patch_accepts_valid_values(db_client):
    """值域校验不能误伤正常输入(含 null = 清除)。"""
    name = "qwen3_8_27b_abliterated_awq"
    r = await db_client.patch(f"/api/v1/engines/{name}/launch-params", json={
        "max_model_len": 32768, "max_num_seqs": 8, "max_num_batched_tokens": 8192,
        "enable_prefix_caching": False, "dtype": "auto",
    })
    assert r.status_code == 200, r.text
    r = await db_client.patch(f"/api/v1/engines/{name}/launch-params",
                              json={"dtype": None, "max_model_len": None})
    assert r.status_code == 200, r.text


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
