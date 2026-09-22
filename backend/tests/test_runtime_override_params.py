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
