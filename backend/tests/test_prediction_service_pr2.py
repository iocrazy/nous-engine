"""服务层 API spec PR-2:统一 prediction 契约纯逻辑 + 端点 wiring。

apply_inputs_to_snapshot(旧 /run 丢弃 inputs 的修复)/ task_to_prediction(ExecutionTask→Cog 形)/
Prefer 头解析。端到端(发布→POST predictions→轮询)真机验。CI 安全(无 torch/DB)。
"""
from __future__ import annotations

import pathlib
import types

from src.services.prediction_service import (
    apply_inputs_to_snapshot,
    prediction_status,
    snapshot_to_executor_form,
    task_to_prediction,
)

_SRC = pathlib.Path(__file__).parent.parent / "src"


# ---- apply_inputs_to_snapshot ----

def test_inject_into_dict_snapshot():
    snap = {"nodes": {
        "in": {"class_type": "text_input", "inputs": {"value": "旧"}},
        "ks": {"class_type": "ksampler", "inputs": {"steps": 20}},
    }}
    exposed = [
        {"node_id": "in", "key": "prompt", "input_name": "value"},
        {"node_id": "ks", "key": "steps", "input_name": "steps"},
    ]
    out = apply_inputs_to_snapshot(snap, exposed, {"prompt": "新提示", "steps": 30})
    assert out["nodes"]["in"]["inputs"]["value"] == "新提示"
    assert out["nodes"]["ks"]["inputs"]["steps"] == 30
    # 原快照不被改(深拷贝)
    assert snap["nodes"]["in"]["inputs"]["value"] == "旧"


def test_missing_key_keeps_default():
    snap = {"nodes": {"in": {"inputs": {"value": "冻结默认"}}}}
    exposed = [{"node_id": "in", "key": "prompt", "input_name": "value"}]
    out = apply_inputs_to_snapshot(snap, exposed, {})  # 不传 prompt
    assert out["nodes"]["in"]["inputs"]["value"] == "冻结默认"


def test_inject_into_list_snapshot_data_form():
    snap = {"nodes": [{"id": "in", "type": "text_input", "data": {"value": "旧"}}]}
    exposed = [{"node_id": "in", "key": "prompt", "input_name": "value"}]
    out = apply_inputs_to_snapshot(snap, exposed, {"prompt": "X"})
    assert out["nodes"][0]["data"]["value"] == "X"


def test_legacy_alias_keys():
    snap = {"nodes": {"n": {"inputs": {}}}}
    exposed = [{"node_id": "n", "api_name": "p", "param_key": "field"}]  # legacy 别名
    out = apply_inputs_to_snapshot(snap, exposed, {"p": "v"})
    assert out["nodes"]["n"]["inputs"]["field"] == "v"


# ---- snapshot_to_executor_form(发布 api-shape dict → executor 编辑形 list)----

def test_snapshot_dict_to_list():
    snap = {"schema": "comfy/api-1", "nodes": {
        "t1": {"class_type": "text_input", "inputs": {"text": "hi"}, "_meta": {"x": 1}},
    }, "edges": [{"a": "b"}]}
    out = snapshot_to_executor_form(snap)
    assert isinstance(out["nodes"], list)
    n = out["nodes"][0]
    assert n["id"] == "t1" and n["type"] == "text_input"
    assert n["data"] == {"text": "hi"} and n["meta"] == {"x": 1}
    assert out["edges"] == [{"a": "b"}]  # edges 原样


def test_snapshot_already_list_passthrough():
    snap = {"nodes": [{"id": "t1", "type": "text_input", "data": {}}]}
    assert snapshot_to_executor_form(snap) is snap


def test_inject_then_convert_carries_value():
    """注入(api-shape inputs)→ 转编辑形(inputs→data),注入值带过去。"""
    snap = {"nodes": {"in": {"class_type": "text_input", "inputs": {"text": "旧"}}}}
    patched = apply_inputs_to_snapshot(snap, [{"node_id": "in", "key": "text", "input_name": "text"}], {"text": "新"})
    exe = snapshot_to_executor_form(patched)
    assert exe["nodes"][0]["data"]["text"] == "新"


# ---- task_to_prediction ----

def _task(**kw):
    base = dict(id=123, workflow_name="my-svc", status="completed", result={"image": "u"},
               error=None, duration_ms=4520, input_json={"prompt": "hi"},
               created_at=None, started_at=None, finished_at=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_prediction_shape_succeeded():
    p = task_to_prediction(_task(), service="my-svc")
    assert p["id"] == "123" and p["service"] == "my-svc"
    assert p["status"] == "succeeded"
    assert p["output"] == {"image": "u"}          # output 只在 succeeded
    assert p["input"] == {"prompt": "hi"}          # input 回显(传入优先,否则 input_json)
    assert p["metrics"]["predict_time"] == 4.52


def test_prediction_status_mapping():
    assert prediction_status("queued") == "starting"
    assert prediction_status("running") == "processing"
    assert prediction_status("failed") == "failed"
    assert prediction_status("cancelled") == "canceled"


def test_prediction_no_output_until_succeeded():
    p = task_to_prediction(_task(status="running", result={"partial": 1}))
    assert p["status"] == "processing"
    assert p["output"] is None                     # 未终态不给 output


def test_prediction_progress_field():
    p = task_to_prediction(_task(status="running", nodes_done=3, nodes_total=5))
    assert p["progress"] == {"nodes_done": 3, "nodes_total": 5}


# ---- fire_webhook (PR-3) ----

import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_fire_webhook_skips_when_no_url():
    from src.services.prediction_service import fire_webhook  # noqa: PLC0415
    # 无 url → 直接返回(不应抛 / 不应尝试 POST)
    await fire_webhook(None, None, "completed", {"id": "1"})


@pytest.mark.asyncio
async def test_fire_webhook_event_filter(monkeypatch):
    from src.services import prediction_service as PS  # noqa: PLC0415
    posted = []

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None): posted.append((url, json))

    import httpx  # noqa: PLC0415
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    # 公网 IP 字面量:过 SSRF 校验(见 test_webhook_ssrf),不依赖 DNS。
    hook = "http://93.184.216.34/cb"
    # filter 不含 start → 跳过
    await PS.fire_webhook(hook, ["completed"], "start", {"id": "1"})
    assert posted == []
    # filter 含 completed → 发,payload = {event, prediction}
    await PS.fire_webhook(hook, ["completed"], "completed", {"id": "1"})
    assert posted == [(hook, {"event": "completed", "prediction": {"id": "1"}})]


def test_workflow_runner_fires_webhook_wiring():
    src = (_SRC / "services/workflow_runner.py").read_text()
    assert "fire_webhook" in src and "_webhook(task" in src
    # start + 终态都发
    assert '_webhook(task, "start")' in src
    assert '_webhook(task, "completed")' in src


# ---- Prefer 头解析 + wiring ----

def test_parse_prefer():
    from src.api.routes.predictions import _parse_prefer  # noqa: PLC0415
    assert _parse_prefer("respond-async") == (True, None)
    assert _parse_prefer("wait=5") == (False, 5.0)
    assert _parse_prefer(None) == (False, None)
    assert _parse_prefer("wait=30, handling=lenient") == (False, 30.0)


def test_route_wired_and_run_deleted():
    pred = (_SRC / "api/routes/predictions.py").read_text()
    assert '"/services/{name}/predictions"' in pred
    assert '"/predictions/{prediction_id}"' in pred
    # 提交核心 2026-09-26 搬到 src/api/prediction_submit.py(与 /v1/skill-runs/generate 共用)。
    assert "submit_prediction(" in pred
    submit = (_SRC / "api/prediction_submit.py").read_text()
    assert "apply_inputs_to_snapshot(" in submit
    assert "validate_service_input(" in submit  # 接进 PR-1 校验
    # 旧 /run 已删(clean cut);legacy rip 进一步删掉整个 instance_service.py(/synthesize)。
    assert not (_SRC / "api/routes/instance_service.py").exists()
    # router 注册
    main = (_SRC / "api/main.py").read_text()
    assert "predictions_routes.router" in main
