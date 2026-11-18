from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api.routes.comfy_templates import MappingBody, _mapping_to_exposed_input
from src.services.nodes import comfy_bridge
from src.services.service_schema import build_service_io_schema, validate_service_input


ARTIFACTS = Path(__file__).resolve().parents[3] / "docs/replications/qwen21-all-in-one"
WORKFLOW = json.loads((ARTIFACTS / "qwen21-all-in-one.api.json").read_text())
MAPPING = json.loads((ARTIFACTS / "qwen21-all-in-one.mapping.json").read_text())
PARAMS = MappingBody.model_validate(MAPPING).exposed_params
EXPOSED = [_mapping_to_exposed_input(param) for param in PARAMS]
SERVICE = "nous-qwen21-all-in-one"


def _active_nodes(graph, node_id):
    node = graph[node_id]
    inputs = node["inputs"]
    if node["class_type"] == "ComfySwitchNode":
        condition = inputs["switch"]
        if isinstance(condition, list):
            toggle = graph[condition[0]]["inputs"]
            if "string_a" in toggle:
                mode = graph[toggle["string_a"][0]]["inputs"]["value"]
                condition = mode == toggle["string_b"]
            else:
                condition = toggle["value"]
        values = [inputs["switch"], inputs.get("on_true" if condition else "on_false")]
    else:
        values = inputs.values()
    active = {node_id}
    for value in values:
        if isinstance(value, list):
            active.update(_active_nodes(graph, value[0]))
    return active


@pytest.mark.parametrize(("mode", "samplers", "uses_optimizer", "uses_seedvr"), [
    ("text", {"24"}, True, False),
    ("reference", {"24"}, True, False),
    ("edit", {"70"}, False, False),
    ("outpaint", {"197"}, False, False),
    ("remove", {"259"}, False, False),
    ("upscale", set(), False, True),
])
def test_modes_only_run_the_selected_branch(mode, samplers, uses_optimizer, uses_seedvr):
    graph = deepcopy(WORKFLOW)
    graph["400"]["inputs"]["value"] = mode
    active = _active_nodes(graph, "389")
    assert active & {"24", "70", "197", "259"} == samplers
    assert ("34" in active or "409" in active) == uses_optimizer
    assert ("344" in active) == (mode == "reference")
    assert ("387" in active) == uses_seedvr
    assert ("339" in active) == bool(samplers)
    assert ("280" in active) == (mode == "remove")
    assert {nid for nid, node in graph.items() if node["class_type"] == "SaveImage"} == {"389"}
    if mode == "edit":
        graph["408"]["inputs"]["value"] = True
        active = _active_nodes(graph, "389")
        assert {"70", "383", "387"} <= active


def test_reference_and_text_optimization_use_the_local_nous_connector():
    assert all(node["class_type"] != "QwenImagePromptOptimizer" for node in WORKFLOW.values())
    for node_id in ("34", "409"):
        node = WORKFLOW[node_id]
        assert node["class_type"] == "CustomSystemPromptGenerator|Mie"
        connector_id, _ = node["inputs"]["llm_service_connector"]
        assert WORKFLOW[connector_id]["inputs"]["api_url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert "source" not in WORKFLOW["409"]["inputs"]


@pytest.mark.parametrize(("payload", "missing"), [
    ({}, {"prompt"}),
    ({"input_mode": None}, {"prompt"}),
    ({"prompt": "  "}, {"prompt"}),
    ({"prompt": "雨后街景"}, set()),
    ({"input_mode": "reference", "image": "ref.png"}, set()),
    ({"input_mode": "edit", "prompt": "改成夜景"}, {"image"}),
    ({"input_mode": "outpaint", "image": ""}, {"image"}),
    ({"input_mode": "remove", "image": "ref.png"}, {"mask"}),
    ({"input_mode": "upscale", "image": "ref.png"}, set()),
])
def test_mode_requirements(payload, missing):
    schema = build_service_io_schema(EXPOSED, [], {})["input_schema"]
    assert set(validate_service_input(schema, payload)) == {f"missing required input: {key}" for key in missing}


@pytest.mark.asyncio
async def test_import_and_read_back_playground_schema(client):
    response = await client.post("/api/v1/comfy-templates", json={
        "name": SERVICE, "workflow": WORKFLOW, "output_kind": "image",
    })
    assert response.status_code == 201, response.text
    path = f"/api/v1/comfy-templates/{response.json()['id']}"
    response = await client.put(f"{path}/mapping", json=MAPPING)
    assert response.status_code == 200, response.text
    detail = (await client.get(path)).json()
    assert MappingBody.model_validate({"exposed_params": detail["exposed_params"]}).exposed_params == PARAMS
    for param in PARAMS:
        assert param.comfy_input in WORKFLOW[param.comfy_node_id]["inputs"]

    response = await client.get(f"/v1/services/{SERVICE}/schema")
    assert response.status_code == 200, response.text
    schema = response.json()
    props = schema["input_schema"]["properties"]
    assert props["input_mode"]["x-widget"] == "radio"
    assert props["input_mode"]["enum"] == ["text", "reference", "edit", "outpaint", "remove", "upscale"]
    assert props["mask"]["x-required-when"] == props["mask"]["x-visible-when"] == {"input_mode": "remove"}
    assert set(schema["output_schema"]["properties"]) == {"image_url"}
    response = await client.post(f"/v1/services/{SERVICE}/predictions", json={"input": {
        "input_mode": "remove", "image": "source.png",
    }})
    assert response.status_code == 422, response.text
    assert "missing required input: mask" in response.text


@pytest.mark.asyncio
async def test_single_reference_edit_prunes_other_loaders_and_submits_one_output(monkeypatch):
    sidecar = SimpleNamespace(
        upload_image=AsyncMock(return_value="uploaded.png"),
        submit=AsyncMock(return_value="edit"),
        wait=AsyncMock(return_value={
            "outputs": {"389": {"images": [{"filename": "result.png", "type": "output"}]}},
            "status": {"status_str": "success"},
        }),
        download=AsyncMock(return_value=b"image"),
    )
    object_info = {
        "ImageScaleToTotalPixels": {"input": {"required": {"image": ["IMAGE"]}}},
        "TextEncodeQwenImage21GH": {"input": {"optional": {f"image_{i:02d}": ["IMAGE"] for i in range(2, 7)}}},
        "ComfySwitchNode": {"input": {"optional": {"on_true": ["*"], "on_false": ["*"]}}},
        "GH_MaskCropV2": {"input": {"required": {"遮罩": ["MASK"]}}},
    }
    monkeypatch.setattr(comfy_bridge, "load_template", AsyncMock(return_value=(WORKFLOW, EXPOSED)))
    monkeypatch.setattr(comfy_bridge, "get_node_infos", AsyncMock(return_value=object_info))
    monkeypatch.setattr(comfy_bridge, "get_client", lambda: sidecar)
    monkeypatch.setattr(comfy_bridge, "write_media", AsyncMock(return_value={"url": "/result.png"}))
    result = await comfy_bridge.ComfyUIWorkflowNode().invoke({
        "template_id": "test", "input_mode": "edit", "prompt": "改成夜景",
        "image": "data:image/png;base64,aW1hZ2U=", "enable_optimization": False, "seed": 0,
    }, {})
    submitted = sidecar.submit.call_args.args[0]
    assert result["image_url"] == "/result.png"
    assert submitted["54"]["inputs"]["image"] == "uploaded.png"
    assert submitted["406"]["inputs"]["value"] == 0
    assert not set(submitted["125"]["inputs"]) & {f"image_{i:02d}" for i in range(2, 7)}
    assert not set(submitted["375"]["inputs"]) & {f"images.image{i}" for i in range(2, 7)}
    assert sidecar.upload_image.await_count == 1
