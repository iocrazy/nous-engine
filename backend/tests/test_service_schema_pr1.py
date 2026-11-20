"""服务层 API spec PR-1:per-service I/O JSON-Schema 生成 + 调用期校验。

build_service_io_schema:exposed_inputs/outputs + 节点 widget 定义 → JSON-Schema。
validate_service_input:手写校验(required/type/enum/min-max)。CI 安全(无 torch/DB)。
"""
from __future__ import annotations

import pathlib

import nodes as NODES
from src.services.service_schema import build_service_io_schema, validate_service_input

_SRC = pathlib.Path(__file__).parent.parent / "src"


def _snapshot():
    # snapshot 两种形之一:{"nodes": {id: {class_type}}}
    return {"nodes": {
        "ks": {"class_type": "ksampler", "inputs": {}},
        "in": {"class_type": "text_input", "inputs": {}},
    }}


def _fake_defs():
    return {
        "ksampler": {"widgets": [
            {"name": "steps", "widget": "slider", "min": 1, "max": 100, "default": 20},
            {"name": "sampler", "widget": "select", "options": ["Euler", "DPM++"], "default": "Euler"},
            {"name": "cfg", "widget": "slider", "min": 0, "max": 20, "default": 4},
        ]},
        "text_input": {"widgets": [
            {"name": "value", "widget": "string", "default": ""},
        ]},
    }


def test_schema_from_widgets(monkeypatch):
    monkeypatch.setattr(NODES, "get_all_definitions", _fake_defs)
    exposed_in = [
        {"node_id": "in", "key": "prompt", "input_name": "value", "type": "string", "required": True, "label": "提示词"},
        {"node_id": "ks", "key": "steps", "input_name": "steps", "type": "number", "required": False},
        {"node_id": "ks", "key": "sampler", "input_name": "sampler", "type": "string", "required": False},
    ]
    exposed_out = [{"node_id": "out", "key": "image", "input_name": "image", "type": "image", "label": "图"}]
    res = build_service_io_schema(exposed_in, exposed_out, _snapshot())
    ins = res["input_schema"]
    assert ins["type"] == "object"
    # prompt:string + description from label
    assert ins["properties"]["prompt"] == {"type": "string", "description": "提示词", "default": ""}
    # steps:slider → number + min/max + default
    assert ins["properties"]["steps"] == {"type": "number", "minimum": 1, "maximum": 100, "default": 20}
    # sampler:select → enum(string)
    assert ins["properties"]["sampler"]["enum"] == ["Euler", "DPM++"]
    assert ins["properties"]["sampler"]["type"] == "string"
    # required 只含 prompt
    assert ins["required"] == ["prompt"]
    # output:image → string + format uri(交付契约)
    assert res["output_schema"]["properties"]["image"] == {"type": "string", "format": "uri", "description": "图"}


def test_schema_fallback_to_exposed_type_when_no_widget(monkeypatch):
    monkeypatch.setattr(NODES, "get_all_definitions", lambda: {})  # 无定义
    exposed_in = [
        {"node_id": "x", "key": "n", "input_name": "n", "type": "int", "required": True},
        {"node_id": "x", "key": "flag", "input_name": "flag", "type": "bool", "required": False},
    ]
    res = build_service_io_schema(exposed_in, [], {"nodes": {}})
    assert res["input_schema"]["properties"]["n"]["type"] == "integer"
    assert res["input_schema"]["properties"]["flag"]["type"] == "boolean"


def test_numeric_select_becomes_number(monkeypatch):
    monkeypatch.setattr(NODES, "get_all_definitions",
                        lambda: {"n": {"widgets": [{"name": "w", "widget": "select", "options": [1, 2, 3]}]}})
    res = build_service_io_schema(
        [{"node_id": "a", "key": "w", "input_name": "w"}], [], {"nodes": {"a": {"class_type": "n"}}})
    p = res["input_schema"]["properties"]["w"]
    assert p["type"] == "number" and p["enum"] == [1, 2, 3]


# ---- validate_service_input ----

def test_validate_required_and_types():
    schema = {"type": "object", "properties": {
        "prompt": {"type": "string"},
        "steps": {"type": "number", "minimum": 1, "maximum": 100},
        "sampler": {"type": "string", "enum": ["Euler", "DPM++"]},
    }, "required": ["prompt"]}

    assert validate_service_input(schema, {"prompt": "hi", "steps": 20, "sampler": "Euler"}) == []
    assert "missing required input: prompt" in validate_service_input(schema, {"steps": 20})
    assert any("expected string" in e for e in validate_service_input(schema, {"prompt": 5}))
    assert any("must be one of" in e for e in validate_service_input(schema, {"prompt": "x", "sampler": "Bad"}))
    assert any(">= 1" in e for e in validate_service_input(schema, {"prompt": "x", "steps": 0}))
    assert any("<= 100" in e for e in validate_service_input(schema, {"prompt": "x", "steps": 999}))


def test_validate_passthrough_extra_and_non_object():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
    # 多余字段放过
    assert validate_service_input(schema, {"a": "x", "extra": 123}) == []
    # 非 object
    assert validate_service_input(schema, ["not", "obj"]) == ["input must be a JSON object"]


def test_required_oldx_inputs_do_not_block_mask_removal():
    when = {"input_mode": ["oldx", "character_card"]}
    schema = build_service_io_schema([
        {"key": "input_mode", "type": "string", "required": False, "default": "remove"},
        {"key": "lora_02_514", "type": "string", "required": True, "constraints": {"visible_when": when}},
        {"key": "strength_02_514", "type": "number", "required": True, "constraints": {"visible_when": when}},
    ], [], {})["input_schema"]
    assert "required" not in schema
    assert schema["properties"]["lora_02_514"]["x-required-when"] == when
    assert validate_service_input(schema, {"input_mode": "remove"}) == []
    assert validate_service_input(schema, {}) == []
    assert validate_service_input(schema, {"input_mode": "oldx"}) == [
        "missing required input: lora_02_514", "missing required input: strength_02_514",
    ]


def test_endpoint_wired():
    # schema 端点在 /v1(predictions 路由),不在 /api/v1(services,被 admin cookie 门拦)。
    src = (_SRC / "api/routes/predictions.py").read_text()
    assert '"/services/{name}/schema"' in src
    assert "build_service_io_schema(" in src
