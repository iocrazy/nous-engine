"""GET /engines/{name}/launch-params —— 读当前生效的启动参数 + 哪些是运行时覆盖的。

写端点(PATCH)在 test_launch_params_endpoint.py;这里只测读。
"""
import pytest

from src.services import runtime_override_store


@pytest.mark.asyncio
async def test_get_launch_params_unknown_engine_404(db_client):
    r = await db_client.get("/api/v1/engines/no_such_engine/launch-params")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_launch_params_returns_yaml_values_when_no_override(db_client):
    from src.config import load_model_configs

    name = "qwen3_8_27b_uncensored_fp8"
    cfgs = load_model_configs()
    if name not in cfgs:
        pytest.skip(f"{name} 不在本机 models.d")

    r = await db_client.get(f"/api/v1/engines/{name}/launch-params")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == name
    # yaml 里 max_model_len = 262144;没覆盖过 → overridden 里不该有它
    assert body["effective"]["max_model_len"] == cfgs[name]["params"]["max_model_len"]
    assert "max_model_len" not in body["overridden"]
    # 白名单外的键(gpu_memory_utilization)**不出现**在 effective 里 —— 它不可编辑,
    # 露出来会诱导用户以为能改(露给前端 = 迟早有人加输入框)。
    assert "gpu_memory_utilization" not in body["effective"]


@pytest.mark.asyncio
async def test_get_launch_params_reads_prefix_caching_from_vllm_args(db_client):
    """prefix caching 写在 `params.vllm_args["enable-prefix-caching"]` 里也要读到。

    本机 qwen3.8 两个变体都是这么配的。只读 `params.enable_prefix_caching` 的话
    UI 复选框会显示"没开"而实际开着 —— 用户一点就把真实状态反转了。
    """
    from src.config import load_model_configs

    name = "qwen3_8_27b_uncensored_fp8"
    cfgs = load_model_configs()
    if name not in cfgs:
        pytest.skip(f"{name} 不在本机 models.d")
    va = (cfgs[name].get("params") or {}).get("vllm_args") or {}
    if "enable-prefix-caching" not in va:
        pytest.skip("该模型 yaml 未用 vllm_args 配 prefix caching")

    r = await db_client.get(f"/api/v1/engines/{name}/launch-params")
    assert r.json()["effective"]["enable_prefix_caching"] is True


@pytest.mark.asyncio
async def test_get_launch_params_editable_lists_what_adapter_accepts(db_client):
    """`editable` = 该引擎的适配器真吃得下的键。前端据此决定渲染哪些控件。"""
    from src.config import load_model_configs

    name = "qwen3_8_27b_uncensored_fp8"
    if name not in load_model_configs():
        pytest.skip(f"{name} 不在本机 models.d")

    r = await db_client.get(f"/api/v1/engines/{name}/launch-params")
    assert r.status_code == 200
    editable = set(r.json()["editable"])
    assert editable >= {
        "max_model_len", "max_num_seqs", "max_num_batched_tokens",
        "enable_prefix_caching", "dtype", "quantization",
    }


@pytest.mark.asyncio
async def test_get_launch_params_editable_empty_for_kwargs_adapter(db_client):
    """MOSS ASR 的 `SGLangOmniAdapter.__init__` 以 `**kwargs` 收尾,一个都不消费 →
    `editable` 空 → 前端整个面板不渲染(而不是给一堆点了不管用的输入框)。"""
    from src.config import load_model_configs

    name = "moss_transcribe_diarize"
    if name not in load_model_configs():
        pytest.skip(f"{name} 不在本机 models.d")

    r = await db_client.get(f"/api/v1/engines/{name}/launch-params")
    assert r.status_code == 200
    assert r.json()["editable"] == []


@pytest.mark.asyncio
async def test_get_launch_params_marks_overridden_keys(db_session, db_client):
    from src.config import load_model_configs

    name = "qwen3_8_27b_uncensored_fp8"
    if name not in load_model_configs():
        pytest.skip(f"{name} 不在本机 models.d")

    await runtime_override_store.set_override(db_session, name, "params", {"max_num_seqs": 3})
    try:
        r = await db_client.get(f"/api/v1/engines/{name}/launch-params")
        assert r.status_code == 200
        body = r.json()
        assert body["effective"]["max_num_seqs"] == 3
        assert body["overridden"] == ["max_num_seqs"]
    finally:
        await runtime_override_store.set_override(db_session, name, "params", {})
