"""Convert the Qwen all-in-one template to four Power LoRA lists."""

import json
import re
import sys
from copy import deepcopy

from migrate_model_input import model_conditions

POWER = "Power Lora Loader (rgthree)"
STACK = "Lora Loader Stack (rgthree)"
BRANCHES = (
    ("qwen_loras", "Qwen 生图 LoRA", "773", "774", ["text", "reference"]),
    ("moss_loras", "MOSS 风格 LoRA", "397", "775", ["text", "reference"]),
    ("edit_loras", "Qwen 编辑 LoRA", "166", "776", "edit"),
    ("oldx_loras", "OLDX 人物替换 LoRA", "514", "777", ["oldx", "character_card"]),
)


def migrate_loras(workflow, params, canvas=None):
    graph, fields, layout = deepcopy((workflow, params, canvas))
    if "773" not in graph:
        qwen_input = graph["17"]["inputs"]
        graph["773"] = {
            "class_type": STACK,
            "inputs": {"model": qwen_input["model"]},
            "_meta": {"title": "Qwen 生图 LoRA"},
        }
        qwen_input["model"] = ["773", 0]

    bindings = {(field["comfy_node_id"], field["comfy_input"]): field for field in fields}

    def rows_for(node_id):
        inputs = graph[node_id]["inputs"]
        for key in sorted(inputs, key=lambda value: (len(value), value)):
            if not re.fullmatch(r"lora_\d+", key):
                continue
            raw = inputs[key]
            mapping = bindings.get((node_id, key), {})
            if isinstance(raw, dict):
                yield raw
                continue
            preset = mapping.get("default", raw)
            strength_key = key.replace("lora_", "strength_", 1)
            strength = bindings.get((node_id, strength_key), {}).get("default", inputs.get(strength_key, 1))
            if preset and preset != "None":
                yield {"lora": preset, "on": True, "strength": strength}
            else:
                options = [option.get("value") if isinstance(option, dict) else option for option in mapping.get("options") or []]
                files = [option for option in options if isinstance(option, str) and option not in ("", "None")]
                if mapping.get("key") == "moss_lora_film" and len(files) > 1:
                    files = [file for file in files if file.rsplit("/", 1)[-1] == "TJFilmMossV2.safetensors"]
                if len(files) == 1:
                    yield {"lora": files[0], "on": False, "strength": strength}

    replaced = set()
    for key, label, base, extension, modes in BRANCHES:
        node = graph[base]
        if node["class_type"] not in (STACK, POWER):
            raise ValueError(f"节点 #{base} 的类型不符合预期")
        rows = list(rows_for(base))
        if extension in graph:
            extra = graph[extension]
            if extra["class_type"] != STACK or extra["inputs"].get("model") != [base, 0]:
                raise ValueError(f"节点 #{extension} 的类型或连线不符合预期")
            rows.extend(rows_for(extension))
            for target in graph.values():
                for name, value in target["inputs"].items():
                    if isinstance(value, list) and len(value) == 2 and value[0] == extension:
                        target["inputs"][name] = [base, value[1]]
            del graph[extension]
            replaced.add(extension)

        node["class_type"] = POWER
        inputs = {k: v for k, v in node["inputs"].items() if k in ("model", "clip")}
        inputs.update({f"lora_{index}": row for index, row in enumerate(rows, 1)})
        node["inputs"] = inputs
        fields = [field for field in fields if field["comfy_node_id"] not in {base, extension}]
        fields.append({
            "key": key, "label": label, "type": "lora_list",
            "comfy_node_id": base, "comfy_input": "loras", "required": False,
            "default": rows, "visible_when": {"input_mode": modes},
        })
        model_conditions(fields[-1])

    if layout:
        layout.pop("source", None)
        loader_ids = {branch[2] for branch in BRANCHES}
        layout["nodes"] = [
            {**node, "type": POWER} if node["id"] in loader_ids else node
            for node in layout["nodes"] if node["id"] not in replaced
        ]
    return graph, fields, layout


if __name__ == "__main__":
    template = json.load(sys.stdin)
    graph, fields, canvas = migrate_loras(
        template["workflow_json"], template["exposed_params"], template.get("canvas_layout"),
    )
    payload = {"workflow": graph, "mapping": {"exposed_params": fields}, "revision": template["revision"]}
    if canvas:
        payload["canvas_layout"] = canvas
    json.dump(payload, sys.stdout, ensure_ascii=False)
