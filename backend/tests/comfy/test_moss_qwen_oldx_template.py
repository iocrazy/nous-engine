import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.api.routes.comfy_templates import MappingBody, _mapping_to_exposed_input
from src.services.comfy.graph_prune import prune_graph
from src.services.service_schema import build_service_io_schema, validate_service_input


ARTIFACTS = Path(__file__).resolve().parents[3] / "docs/replications"
PACKAGE = ARTIFACTS / "moss-qwen-oldx"
WORKFLOW = json.loads((PACKAGE / "workflow.api.json").read_text())
MAPPING = json.loads((PACKAGE / "mapping.json").read_text())
CANVAS = json.loads((PACKAGE / "canvas.json").read_text())
PARAMS = MappingBody.model_validate(MAPPING).exposed_params
SCHEMA = build_service_io_schema([_mapping_to_exposed_input(param) for param in PARAMS], [], {})["input_schema"]


def active_nodes(graph):
    seen = set()

    def resolve(value):
        if not isinstance(value, list):
            return value
        node = graph[value[0]]
        inputs = node["inputs"]
        match node["class_type"]:
            case "StringCompare":
                return resolve(inputs["string_a"]) == resolve(inputs["string_b"])
            case "ComfySwitchNode":
                return resolve(inputs.get("on_true" if resolve(inputs["switch"]) else "on_false"))
            case _:
                return inputs["value"]

    def visit(node_id):
        if node_id in seen:
            return
        seen.add(node_id)
        node = graph[node_id]
        inputs = node["inputs"]
        values = inputs.values()
        if node["class_type"] == "ComfySwitchNode":
            values = [inputs["switch"], inputs.get("on_true" if resolve(inputs["switch"]) else "on_false")]
        for value in values:
            if isinstance(value, list):
                visit(value[0])

    visit("389")
    return seen


@pytest.mark.parametrize(("mode", "sampler"), [
    ("text", "24"), ("reference", "24"), ("edit", "70"),
    ("oldx", "522"), ("character_card", "522"), ("outpaint", "197"),
    ("remove", "259"), ("upscale", None),
])
def test_mode_runs_just_its_sampler(mode, sampler):
    graph = deepcopy(WORKFLOW)
    graph["700"]["inputs"]["value"] = mode
    active = active_nodes(graph)
    assert active & {"404", "24", "70", "522", "197", "259"} == ({sampler} if sampler else set())
    assert ("387" in active) == (mode == "upscale")
    assert ("344" in active) == (mode == "reference")
    assert ("654" in active) == (mode == "character_card")
    assert ("711" in active) == (mode == "oldx")
    assert ("280" in active) == (mode == "remove")
    assert {node_id for node_id, node in graph.items() if node["class_type"] == "SaveImage"} == {"389"}


def test_edit_aspect_ratio_targets_the_edit_resolution_node():
    ratio = next(param for param in PARAMS if param.key == "edit_aspect_ratio")
    prop = SCHEMA["properties"][ratio.key]
    assert prop["x-visible-when"] == {"input_mode": "edit"}
    assert prop["default"] == WORKFLOW[ratio.comfy_node_id]["inputs"][ratio.comfy_input] == "原始比例"
    assert prop["enum"] == SCHEMA["properties"]["aspect_ratio"]["enum"]
    for mode, expected in (("edit", True), ("text", False), ("reference", False)):
        graph = deepcopy(WORKFLOW)
        graph["700"]["inputs"]["value"] = mode
        assert (ratio.comfy_node_id in active_nodes(graph)) == expected
    payload = {"input_mode": "edit", "prompt": "Make this a portrait", "image": "source.png"}
    assert validate_service_input(SCHEMA, {**payload, ratio.key: "9:16"}) == []
    assert validate_service_input(SCHEMA, {**payload, ratio.key: "99:1"})


@pytest.mark.parametrize("mode", ["text", "reference"])
def test_generation_can_use_moss(mode):
    graph = deepcopy(WORKFLOW)
    graph["700"]["inputs"]["value"] = mode
    graph["704"]["inputs"]["value"] = "moss-image"
    active = active_nodes(graph)
    assert {"435", "404"} <= active
    assert ("344" in active) == (mode == "reference")
    assert "24" not in active


def test_oldx_can_upscale():
    graph = deepcopy(WORKFLOW)
    graph["700"]["inputs"]["value"] = "oldx"
    graph["706"]["inputs"]["value"] = True
    assert {"522", "387", "719"} <= active_nodes(graph)


def test_optional_images_are_pruned_without_removing_the_output():
    optional = [param.comfy_node_id for param in PARAMS if param.omit_when_empty]
    info = {
        "ImageScaleToTotalPixels": {"input": {"required": {"image": ["IMAGE"]}}},
        "TextEncodeQwenImage21GH": {"input": {"optional": {f"image_{i:02d}": ["IMAGE"] for i in range(2, 7)}}},
        "ComfySwitchNode": {"input": {"optional": {"on_true": ["*"], "on_false": ["*"]}}},
    }
    pruned = prune_graph(WORKFLOW, optional, info)
    assert "389" in pruned.graph
    assert not (set(optional) & pruned.graph.keys())
    assert "image_03" not in pruned.graph["534"]["inputs"]
    assert "image_02" not in pruned.graph["125"]["inputs"]
    assert "images.image3" not in pruned.graph["539"]["inputs"]


@pytest.mark.parametrize(("payload", "missing"), [
    ({}, {"prompt"}),
    ({"input_mode": "oldx"}, {"image", "oldx_target"}),
    ({"input_mode": "oldx", "image": "portrait.png", "oldx_target": "scene.png"}, set()),
    ({"input_mode": "character_card", "image": "portrait.png"}, set()),
    ({"input_mode": "remove", "image": "photo.png"}, {"mask"}),
])
def test_mode_requirements(payload, missing):
    assert set(validate_service_input(SCHEMA, payload)) == {f"missing required input: {key}" for key in missing}


async def test_replace_existing_service_without_changing_its_route(client):
    old = ARTIFACTS / "qwen21-all-in-one"
    name = "nous-qwen21-all-in-one"
    created = await client.post("/api/v1/comfy-templates", json={
        "name": name, "output_kind": "image",
        "workflow": json.loads((old / "qwen21-all-in-one.api.json").read_text()),
    })
    assert created.status_code == 201
    template = f"/api/v1/comfy-templates/{created.json()['id']}"
    original = await client.put(f"{template}/mapping", json=json.loads((old / "qwen21-all-in-one.mapping.json").read_text()))
    assert original.status_code == 200
    res = await client.put(template, json={
        "revision": original.json()["revision"], "workflow": WORKFLOW, "canvas_layout": CANVAS, "mapping": MAPPING,
    })
    assert res.status_code == 200, res.text
    assert res.json()["stale_keys"] == []
    schema = (await client.get(f"/v1/services/{name}/schema")).json()
    assert schema["input_schema"]["properties"]["input_mode"]["default"] == "text"
    assert set(schema["input_schema"]["properties"]["input_mode"]["enum"]) == set(SCHEMA["properties"]["input_mode"]["enum"])
    assert schema["input_schema"]["additionalProperties"] is False
    assert schema["input_schema"]["properties"]["model"]["enum"] == ["qwen-image-2.1-turbo", "moss-image"]
    assert "reference_engine" not in schema["input_schema"]["properties"]
    assert "moss_text" not in schema["input_schema"]["properties"]["input_mode"]["enum"]
    assert set(schema["output_schema"]["properties"]) == {"image_url"}
    old_input = await client.post(f"/v1/apps/{name}/run", json={"lora_01": "slot.safetensors"})
    assert old_input.status_code == 422
    assert "lora_01" in old_input.text
    for key, payload in [
        ("input_mode", {"input_mode": "moss_text", "prompt": "example"}),
        ("reference_engine", {"input_mode": "reference", "reference_engine": "moss", "image": "example.png"}),
        ("model", {"model": "qwen", "prompt": "example"}),
        ("model", {"input_mode": "upscale", "model": "moss-image", "image": "example.png"}),
    ]:
        direct = await client.post(f"/v1/apps/{name}/run", json=payload)
        prediction = await client.post(f"/v1/services/{name}/predictions", json={"input": payload})
        assert direct.status_code == prediction.status_code == 422
        assert key in direct.text and key in prediction.text


def test_moss_loras_follow_the_canvas_order_and_enabled_state():
    inputs = WORKFLOW["397"]["inputs"]
    assert [
        (row["lora"], row["on"], row["strength"]) for index in range(1, 4)
        for row in [inputs[f"lora_{index}"]]
    ] == [
        ("moss/KOOK-MOSS-SFT-观澜.safetensors", True, 1),
        ("moss/feverdream-epoch-07.safetensors", True, 0.5),
        ("moss/TJFilmMossV2.safetensors", False, 0.7),
    ]
    params = {param.key: param for param in PARAMS}
    assert params["moss_loras"].comfy_input == "loras"
    assert SCHEMA["properties"]["moss_loras"]["type"] == "array"
    assert "reference" in SCHEMA["properties"]["use_deepseek"]["x-visible-when"]["input_mode"]


@pytest.mark.parametrize(("key", "node", "mode"), [
    ("qwen_loras", "773", "text"),
    ("moss_loras", "397", "text"),
    ("edit_loras", "166", "edit"),
    ("oldx_loras", "514", "oldx"),
])
def test_lora_list_is_bound_to_the_power_loader_in_its_branch(key, node, mode):
    param = next(param for param in PARAMS if param.key == key)
    assert param.comfy_node_id == node
    assert WORKFLOW[node]["class_type"] == "Power Lora Loader (rgthree)"
    graph = deepcopy(WORKFLOW)
    graph["700"]["inputs"]["value"] = mode
    if key == "moss_loras":
        graph["704"]["inputs"]["value"] = "moss-image"
    assert node in active_nodes(graph)
    directory = "moss" if key == "moss_loras" else "qwen2.1"
    rows = [{"lora": f"{directory}/any.safetensors", "on": True, "strength": 0.4}] * 8
    payload = {"input_mode": mode, "prompt": "scene", "image": "image.png", "oldx_target": "target.png", key: rows}
    assert validate_service_input(SCHEMA, payload) == []
    assert validate_service_input(SCHEMA, {**payload, "lora_01": "old.safetensors"})
    assert validate_service_input(SCHEMA, {**payload, key: [{"on": "true", "strength": 20}]})
    assert validate_service_input(SCHEMA, {**payload, key: [{**rows[0], "lora": "other/any.safetensors"}]})
