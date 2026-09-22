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


def test_params_override_on_cfg_without_params_key(monkeypatch):
    """yaml 里没有 params 块的模型也要能被覆盖(别 KeyError)。"""
    monkeypatch.setattr(
        "src.config.load_runtime_overrides",
        lambda: {"m": {"params": {"max_model_len": 4096}}})
    cfgs = {"m": {"id": "m"}}
    _apply_runtime_overrides(cfgs)
    assert cfgs["m"]["params"] == {"max_model_len": 4096}
