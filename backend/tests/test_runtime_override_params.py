"""model_runtime_overrides.params —— 启动参数的运行时覆盖。

三态(与 gpus 的 `[]` 哨兵同源的教训):
  NULL / {}  = 未覆盖 → 回退 models.d yaml 的 params
  {"k": v}   = 只覆盖 k,其余键仍走 yaml
"""
from src.models.model_runtime_override import ModelRuntimeOverride


def test_to_overrides_emits_params_when_set():
    row = ModelRuntimeOverride(model_id="m", params={"max_model_len": 262144})
    assert row.to_overrides() == {"params": {"max_model_len": 262144}}


def test_to_overrides_omits_empty_params():
    """空 dict 与 NULL 都算「没覆盖」——不像 gpus,params 没有「显式清空」的语义需求:
    要清空就是把键删掉,整列回到 NULL。"""
    assert ModelRuntimeOverride(model_id="m", params={}).to_overrides() == {}
    assert ModelRuntimeOverride(model_id="m", params=None).to_overrides() == {}


def test_to_overrides_params_coexists_with_placement():
    row = ModelRuntimeOverride(model_id="m", gpu=1, params={"max_num_seqs": 8})
    out = row.to_overrides()
    assert out["gpu"] == 1
    assert out["params"] == {"max_num_seqs": 8}


import pytest

from src.services import runtime_override_store


@pytest.mark.asyncio
async def test_set_override_params_writes_and_merges(db_session):
    await runtime_override_store.set_override(
        db_session, "m1", "params", {"max_model_len": 262144})
    assert runtime_override_store.get_overrides()["m1"]["params"] == {
        "max_model_len": 262144}

    # 再写第二个键:**合并**而不是替换整个 dict
    await runtime_override_store.set_override(
        db_session, "m1", "params", {"max_num_seqs": 8})
    assert runtime_override_store.get_overrides()["m1"]["params"] == {
        "max_model_len": 262144, "max_num_seqs": 8}


@pytest.mark.asyncio
async def test_set_override_params_none_value_deletes_key(db_session):
    await runtime_override_store.set_override(
        db_session, "m2", "params", {"max_model_len": 262144, "max_num_seqs": 8})
    # 显式 None = 删这个键(回退 yaml),不是"把它设成 null"
    await runtime_override_store.set_override(
        db_session, "m2", "params", {"max_num_seqs": None})
    assert runtime_override_store.get_overrides()["m2"]["params"] == {
        "max_model_len": 262144}


@pytest.mark.asyncio
async def test_set_override_params_empty_clears_column(db_session):
    await runtime_override_store.set_override(
        db_session, "m3", "params", {"max_model_len": 262144})
    await runtime_override_store.set_override(db_session, "m3", "params", {})
    assert "params" not in runtime_override_store.get_overrides().get("m3", {})


from src.config import _apply_runtime_overrides


def test_params_deep_merge_keeps_unoverridden_keys(monkeypatch):
    """只覆盖 max_model_len,max_num_seqs 必须仍是 yaml 的值(不能整体替换 params)。"""
    monkeypatch.setattr(
        "src.config.load_runtime_overrides",
        lambda: {"m": {"params": {"max_model_len": 262144}}})
    cfgs = {"m": {"id": "m", "params": {"max_model_len": 32768, "max_num_seqs": 16}}}
    _apply_runtime_overrides(cfgs)
    assert cfgs["m"]["params"] == {"max_model_len": 262144, "max_num_seqs": 16}


def test_params_merge_does_not_write_through_cache(monkeypatch):
    """copy_before_write=True 时**绝不能**改到原 params dict。

    调用方(model_scanner._with_runtime_overrides)传进来的是某个 TTL 缓存结构的浅拷贝:
    外层 dict 是新的,但 params 子 dict 与缓存共享同一个对象。写穿的症状很阴 ——
    「改了参数 30 秒内看不到」或「改一次污染此后所有读」,难查。
    """
    monkeypatch.setattr(
        "src.config.load_runtime_overrides",
        lambda: {"m": {"params": {"max_model_len": 262144}}})
    cached_params = {"max_model_len": 32768, "max_num_seqs": 16}
    cached_cfg = {"id": "m", "params": cached_params}
    shallow = {"m": dict(cached_cfg)}          # 模拟调用方的浅拷贝

    _apply_runtime_overrides(shallow, copy_before_write=True)

    assert shallow["m"]["params"]["max_model_len"] == 262144, "覆盖没生效"
    assert cached_params["max_model_len"] == 32768, "写穿了缓存里的原 params dict"


def test_prefix_caching_override_drops_vllm_args_alias(monkeypatch):
    """关 prefix caching 时,vllm_args 里的同义写法必须被摘掉。

    两条配法并存:`params.enable_prefix_caching`(适配器 kwarg)和
    `params.vllm_args["enable-prefix-caching"]`(透传)。merge_vllm_args 同名时
    **以 vllm_args 为准** —— 不摘的话覆盖写进了库、GET 也报 false,load 时又被打开:
    开关是单向的(开得了、关不掉,且不报错)。本机 qwen3.8 两个变体走的正是后者。
    """
    monkeypatch.setattr(
        "src.config.load_runtime_overrides",
        lambda: {"m": {"params": {"enable_prefix_caching": False}}})
    cfgs = {"m": {"id": "m", "params": {
        "max_model_len": 32768,
        "vllm_args": {"enable-prefix-caching": True, "reasoning-parser": "qwen3"},
    }}}
    _apply_runtime_overrides(cfgs)

    assert cfgs["m"]["params"]["enable_prefix_caching"] is False
    assert "enable-prefix-caching" not in cfgs["m"]["params"]["vllm_args"]
    # 只摘这一个键,vllm_args 里别的透传参数不能被误伤
    assert cfgs["m"]["params"]["vllm_args"]["reasoning-parser"] == "qwen3"


def test_prefix_caching_override_keeps_vllm_args_when_not_overridden(monkeypatch):
    """没覆盖 enable_prefix_caching 就别动 vllm_args —— 摘别名只在显式覆盖时发生。"""
    monkeypatch.setattr(
        "src.config.load_runtime_overrides",
        lambda: {"m": {"params": {"max_model_len": 4096}}})
    cfgs = {"m": {"id": "m", "params": {
        "vllm_args": {"enable-prefix-caching": True}}}}
    _apply_runtime_overrides(cfgs)
    assert cfgs["m"]["params"]["vllm_args"]["enable-prefix-caching"] is True


def test_prefix_caching_alias_drop_does_not_write_through_cache(monkeypatch):
    """摘别名**绝不能**原地 pop —— vllm_args 比 params 更深一层,同样与调用方的
    TTL 缓存共享对象。写穿的症状是「关过一次之后,此后所有读都看不到这条透传参数」,
    而且 30 秒 TTL 内连重启 UI 都救不回来。照 copy_before_write 那条的形状钉住。
    """
    monkeypatch.setattr(
        "src.config.load_runtime_overrides",
        lambda: {"m": {"params": {"enable_prefix_caching": False}}})
    cached_vllm_args = {"enable-prefix-caching": True, "reasoning-parser": "qwen3"}
    cached_params = {"max_model_len": 32768, "vllm_args": cached_vllm_args}
    shallow = {"m": dict({"id": "m", "params": cached_params})}  # 调用方的浅拷贝

    _apply_runtime_overrides(shallow, copy_before_write=True)

    assert "enable-prefix-caching" not in shallow["m"]["params"]["vllm_args"], "覆盖没生效"
    assert cached_vllm_args["enable-prefix-caching"] is True, "写穿了缓存里的 vllm_args"
    assert cached_params["vllm_args"] is cached_vllm_args


def test_params_merge_filters_non_whitelisted_keys(monkeypatch):
    """读路径也要过白名单,与写路径(`_instantiate_adapter`)对称。

    端点今天拦得住 `tensor_parallel_size`,但库里要是**已经**躺着一条(手写 SQL /
    历史脏数据),这里是唯一一条让它经 `add_from_scan` 渗进 `spec.params` 的路 ——
    而 `_resolve_placement` 正读那个键,放置结论就被数据面改掉了。
    """
    monkeypatch.setattr(
        "src.config.load_runtime_overrides",
        lambda: {"m": {"params": {"tensor_parallel_size": 8, "max_model_len": 4096}}})
    cfgs = {"m": {"id": "m", "params": {}}}
    _apply_runtime_overrides(cfgs)
    assert cfgs["m"]["params"] == {"max_model_len": 4096}


def test_params_override_on_cfg_without_params_key(monkeypatch):
    """yaml 里没有 params 块的模型也要能被覆盖(别 KeyError)。"""
    monkeypatch.setattr(
        "src.config.load_runtime_overrides",
        lambda: {"m": {"params": {"max_model_len": 4096}}})
    cfgs = {"m": {"id": "m"}}
    _apply_runtime_overrides(cfgs)
    assert cfgs["m"]["params"] == {"max_model_len": 4096}
