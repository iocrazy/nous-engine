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
    from src.config import LAUNCH_PARAM_WHITELIST

    forbidden = {"gpu", "gpus", "device", "tensor_parallel_size", "gpu_memory_utilization",
                 "vram_budget", "vram_mb", "kv_cache_dtype"}
    assert not (LAUNCH_PARAM_WHITELIST & forbidden)
    # 路由层的短名必须就是配置层那一个对象 —— 复制一份的话这条机检就只守住了半边。
    assert _LAUNCH_PARAM_WHITELIST is LAUNCH_PARAM_WHITELIST


@pytest.mark.asyncio
async def test_patch_rejects_engine_whose_adapter_cannot_consume(db_client):
    """适配器吃不下这些键的引擎 → 400,而不是静静存进库。

    MOSS ASR 走 `SGLangOmniAdapter`,`__init__` 以 `**kwargs` 收尾,6 个白名单键
    **一个都不消费**。此前 PATCH 是 200:写得进库、GET 报「已覆盖」、引擎行为纹丝不动
    —— 正是这一支从头在修的那类 bug(2026-09-22 复查 N1)。
    """
    from src.config import load_model_configs

    name = "moss_transcribe_diarize"
    if name not in load_model_configs():
        pytest.skip(f"{name} 不在本机 models.d")

    r = await db_client.patch(f"/api/v1/engines/{name}/launch-params",
                              json={"max_model_len": 4096})
    assert r.status_code == 400, r.text
    assert name in r.text                      # 点名是哪个引擎
    assert "SGLangOmniAdapter" in r.text       # 点名是哪个适配器

    # 被拒的请求不能留下痕迹
    from src.services import runtime_override_store
    assert "max_model_len" not in (
        runtime_override_store.get_overrides().get(name, {}).get("params") or {})


@pytest.mark.asyncio
async def test_patch_null_allowed_even_when_adapter_cannot_consume(db_session, db_client):
    """吃不下的键也要能**清**:`null` 是清除覆盖,不是设置。

    拦住 null 的话,库里躺着的历史死数据(收窄之前存下的、到不了引擎的覆盖)就永远
    清不掉 —— 面板不渲染它、PATCH 又拒绝它,只剩手改 DB。清除只会让状态更干净。
    """
    from src.config import load_model_configs
    from src.services import runtime_override_store

    name = "moss_transcribe_diarize"
    if name not in load_model_configs():
        pytest.skip(f"{name} 不在本机 models.d")

    # 绕过端点直接种一条"历史死数据",模拟收窄之前存下的覆盖
    await runtime_override_store.set_override(
        db_session, name, "params", {"max_model_len": 4096})
    assert runtime_override_store.get_overrides()[name]["params"]["max_model_len"] == 4096

    r = await db_client.patch(f"/api/v1/engines/{name}/launch-params",
                              json={"max_model_len": None})
    assert r.status_code == 200, r.text
    assert "max_model_len" not in (
        runtime_override_store.get_overrides().get(name, {}).get("params") or {})


def test_editable_launch_params_by_adapter_signature():
    """可编辑性按**适配器签名**判,不按模型 type 猜。

    只读签名,不实例化、不 load —— 测试绝不能真起推理服务碰 GPU。
    """
    from src.api.routes.engines import _editable_launch_params
    from src.config import LAUNCH_PARAM_WHITELIST

    vllm = _editable_launch_params(
        {"adapter": "src.services.inference.llm_vllm.VLLMAdapter"})
    assert vllm == LAUNCH_PARAM_WHITELIST      # 6 个键全接

    asr = _editable_launch_params(
        {"adapter": "src.services.inference.asr_sglang.SGLangOmniAdapter"})
    assert asr == frozenset()                  # `**kwargs` 不算接受

    assert _editable_launch_params({}) == frozenset()          # 没有 adapter
    assert _editable_launch_params({"adapter": "nodots"}) == frozenset()


def test_editable_launch_params_fails_open_when_signature_unavailable():
    """取不到签名就别拦:拦错了用户连改都改不了,比多给一个无效按钮更糟。"""
    from src.api.routes.engines import _editable_launch_params
    from src.config import LAUNCH_PARAM_WHITELIST

    assert _editable_launch_params(
        {"adapter": "src.no_such_module_at_all.NoSuchAdapter"}
    ) == LAUNCH_PARAM_WHITELIST
    assert _editable_launch_params(
        {"adapter": "src.config.NoSuchClassInThisModule"}
    ) == LAUNCH_PARAM_WHITELIST


@pytest.mark.asyncio
async def test_patch_null_clears_single_key(db_client):
    name = "qwen3_8_27b_abliterated_awq"
    await db_client.patch(f"/api/v1/engines/{name}/launch-params",
                       json={"max_model_len": 16384, "max_num_seqs": 4})
    r = await db_client.patch(f"/api/v1/engines/{name}/launch-params",
                              json={"max_num_seqs": None})
    assert r.status_code == 200
    assert r.json()["params"] == {"max_model_len": 16384}
