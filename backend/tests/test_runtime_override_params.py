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
