"""Convert the registered MOSS/Qwen/OLDX workflow to one model input."""

import json
import sys
from copy import deepcopy

MOSS = "moss-image"
TURBO = "qwen-image-2.1-turbo"
MODEL_OPTIONS = [
    {"value": TURBO, "label": "Qwen Image 2.1 Turbo"},
    {"value": MOSS, "label": "MOSS Image"},
]
MODEL_FIELDS = {
    "moss_aspect_ratio": MOSS, "moss_megapixels": MOSS,
    "moss_steps": MOSS, "moss_cfg": MOSS, "moss_loras": MOSS,
    "aspect_ratio": TURBO, "megapixels": TURBO, "qwen_loras": TURBO,
}


def model_conditions(field):
    if model := MODEL_FIELDS.get(field["key"]):
        field["visible_when"]["model"] = model


def migrate_model_input(workflow, params, canvas=None):
    graph, fields, layout = deepcopy((workflow, params, canvas))
    if (graph["730"]["inputs"].get("string_b") != "moss_text"
            or graph["751"]["inputs"].get("on_false") != ["750", 0]
            or graph["752"]["inputs"].get("on_true") != ["716", 0]):
        raise ValueError("Expected the MOSS/Qwen/OLDX model routing nodes 730, 750, 751 and 752")

    mode = next(field for field in fields if field["key"] == "input_mode")
    model = next(field for field in fields if field["key"] == "reference_engine")
    mode["options"] = [
        {**option, "label": "文字生图"} if option["value"] == "text" else option
        for option in mode["options"] if option["value"] != "moss_text"
    ]
    if mode.get("default") == "moss_text":
        mode["default"] = "text"
    default = MOSS if model.get("default") == "moss" else TURBO
    model.update(key="model", label="生图模型", default=default, options=deepcopy(MODEL_OPTIONS),
                 visible_when={"input_mode": ["text", "reference"]})
    for field in fields:
        for name in ("visible_when", "required_when"):
            conditions = field.get(name) or {}
            modes = conditions.get("input_mode")
            if isinstance(modes, list):
                conditions["input_mode"] = list(dict.fromkeys("text" if key == "moss_text" else key for key in modes))
            elif modes == "moss_text":
                conditions["input_mode"] = "text"
        model_conditions(field)

    if graph["700"]["inputs"].get("value") == "moss_text":
        graph["700"]["inputs"]["value"] = "text"
    graph["704"]["inputs"]["value"] = default
    graph["704"]["_meta"]["title"] = "生图模型"
    graph["705"]["inputs"]["string_b"] = MOSS
    graph["716"]["_meta"]["title"] = "生图输出"
    graph["731"]["_meta"]["title"] = "文字生图"
    graph["751"]["_meta"]["title"] = "文字生图"
    graph["751"]["inputs"].update(on_true=["716", 0], on_false=graph["750"]["inputs"]["on_false"])
    for node in ("730", "750"):
        del graph[node]

    if layout:
        layout["nodes"] = [node for node in layout["nodes"] if node["id"] not in {"730", "750"}]
        layout.pop("source", None)
    return graph, fields, layout


if __name__ == "__main__":
    template = json.load(sys.stdin)
    graph, fields, layout = migrate_model_input(
        template["workflow_json"], template["exposed_params"], template.get("canvas_layout"),
    )
    payload = {"workflow": graph, "mapping": {"exposed_params": fields}, "revision": template["revision"]}
    if layout:
        payload["canvas_layout"] = layout
    json.dump(payload, sys.stdout, ensure_ascii=False)
