from copy import deepcopy
from itertools import product
import json
from pathlib import Path

import pytest

from src.api.routes.comfy_templates import MappingBody
from src.services.comfy.graph_prune import prune_graph


ARTIFACTS = Path(__file__).resolve().parents[3] / "docs/replications/moss-image"
WORKFLOW = json.loads((ARTIFACTS / "moss-image.api.json").read_text())
MAPPING = json.loads((ARTIFACTS / "moss-image.mapping.json").read_text())


def test_mapping_targets_real_inputs_and_limits_loras_to_moss():
    mapping = MappingBody.model_validate(MAPPING)
    assert len({p.key for p in mapping.exposed_params}) == len(mapping.exposed_params)
    for param in mapping.exposed_params:
        assert param.comfy_input in WORKFLOW[param.comfy_node_id]["inputs"]
        if param.key.startswith("lora_"):
            assert param.options == [
                "None",
                "moss/KOOK-MOSS-SFT-观澜.safetensors",
                "moss/TJFilmMossV2.safetensors",
            ]


def test_missing_reference_does_not_remove_the_image_output_chain():
    reference = next(p for p in MAPPING["exposed_params"] if p["key"] == "reference_image")
    assert reference["omit_when_empty"] and not reference["required"]
    assert reference.get("default") is None
    result = prune_graph(WORKFLOW, {reference["comfy_node_id"]}, {
        "CustomSystemPromptGenerator|Mie": {"input": {"optional": {"source": ["IMAGE"]}}},
    })
    assert result.removed == frozenset({"92"})
    assert "source" not in result.graph["66"]["inputs"]
    assert result.graph["80"]["inputs"]["switch"] is False
    assert result.graph["51"]["inputs"]["images"] == ["102", 0]
    assert "9" not in result.graph


def test_prompt_branches_are_lazy_without_extra_llm_output_nodes():
    switch = WORKFLOW["80"]["inputs"]
    assert switch["on_false"] == ["101", 0]
    assert switch["on_true"] == ["66", 0]
    assert all(n["class_type"] != "ShowAnything|Mie" for n in WORKFLOW.values())
    assert WORKFLOW["29"]["inputs"]["text"] == ["80", 0]
    for node in WORKFLOW.values():
        for value in node["inputs"].values():
            if isinstance(value, list):
                assert value[0] in WORKFLOW


def test_lora_stack_feeds_both_model_and_text_encoder():
    stack = WORKFLOW["97"]["inputs"]
    assert stack["model"] == ["28", 0]
    assert stack["clip"] == ["30", 0]
    assert stack["lora_01"] == "moss/KOOK-MOSS-SFT-观澜.safetensors"
    assert stack["strength_01"] == ["111", 0]
    assert all(stack[f"lora_{slot:02d}"] == "None" for slot in range(2, 5))
    assert WORKFLOW["38"]["inputs"]["model"] == ["97", 0]
    assert WORKFLOW["29"]["inputs"]["clip"] == ["97", 1]
    assert WORKFLOW["36"]["inputs"]["clip"] == ["97", 1]


@pytest.mark.asyncio
async def test_imported_service_exposes_lora_controls_and_image_output(client):
    response = await client.post("/api/v1/comfy-templates", json={
        "name": "nous-moss-image", "workflow": WORKFLOW, "output_kind": "image",
    })
    assert response.status_code == 201, response.text
    template_id = response.json()["id"]
    response = await client.put(f"/api/v1/comfy-templates/{template_id}/mapping", json=MAPPING)
    assert response.status_code == 200, response.text
    response = await client.get("/v1/services/nous-moss-image/schema")
    assert response.status_code == 200, response.text
    schema = response.json()
    assert schema["input_schema"]["properties"]["lora_02"]["enum"] == [
        "None", "moss/KOOK-MOSS-SFT-观澜.safetensors", "moss/TJFilmMossV2.safetensors",
    ]
    assert "image_url" in schema["output_schema"]["properties"]
    properties = schema["input_schema"]["properties"]
    assert len(properties) == 28
    for name in ["enable_expansion", "enable_upscale", *[f"enable_lora_{i:02d}" for i in range(1, 5)]]:
        assert properties[name]["type"] == "boolean"
    assert properties["enable_lora_01"]["default"] is True
    assert properties["enable_lora_02"]["default"] is False


def test_deepseek_uses_server_side_credentials():
    connector = WORKFLOW["81"]["inputs"]
    assert connector["api_token"] == ""
    assert connector["config_key"] == "deepseek-flash"
    assert connector["prefer_local_config"] is True
    assert all(n["class_type"] != "QwenTE_ModelLoader" for n in WORKFLOW.values())


def _patch(values):
    graph = deepcopy(WORKFLOW)
    for param in MAPPING["exposed_params"]:
        if param["key"] in values:
            graph[param["comfy_node_id"]]["inputs"][param["comfy_input"]] = values[param["key"]]
    return graph


def _active_nodes(graph, node_id="51"):
    active = {node_id}
    node = graph[node_id]
    inputs = node["inputs"]
    if node["class_type"] == "LazySwitchKJ":
        dependencies = [inputs["on_true" if inputs["switch"] else "on_false"]]
    else:
        dependencies = inputs.values()
    for value in dependencies:
        if isinstance(value, list):
            active.update(_active_nodes(graph, value[0]))
    return active


@pytest.mark.parametrize("expansion,upscale,reference", product([False, True], repeat=3))
def test_switches_skip_unselected_model_branches(expansion, upscale, reference):
    graph = _patch({"enable_expansion": expansion, "enable_upscale": upscale, "use_reference": reference})
    active = _active_nodes(graph)
    assert ("65" in active) == (expansion and not reference)
    assert ("66" in active) == reference
    assert ("81" in active) == (expansion or reference)
    assert ("46" in active) == upscale
    assert ("45" in active) == upscale
    assert ("48" in active) == upscale
    assert "37" in active


@pytest.mark.parametrize("enabled", product([False, True], repeat=4))
def test_each_lora_switch_preserves_its_settings_and_gates_its_strength(enabled):
    values = {}
    for slot, state in enumerate(enabled, 1):
        values.update({
            f"enable_lora_{slot:02d}": state,
            f"lora_{slot:02d}": "moss/TJFilmMossV2.safetensors",
            f"strength_{slot:02d}": slot * 0.25,
        })
    graph = _patch(values)
    for slot, state in enumerate(enabled, 1):
        gate_id, output = graph["97"]["inputs"][f"strength_{slot:02d}"]
        gate = graph[gate_id]["inputs"]
        assert output == 0
        assert gate["on_true"] == slot * 0.25
        assert gate["on_true" if gate["switch"] else "on_false"] == (slot * 0.25 if state else 0)
        assert graph["97"]["inputs"][f"lora_{slot:02d}"] == "moss/TJFilmMossV2.safetensors"
