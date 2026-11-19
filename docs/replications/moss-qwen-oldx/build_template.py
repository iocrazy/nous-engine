"""Build the Nous Engine template from the original canvas and its all-enabled API export."""

import argparse
import json
from copy import deepcopy
from pathlib import Path


HERE = Path(__file__).resolve().parent
OLD = HERE.parent / "qwen21-all-in-one"
MODE_OPTIONS = {
    "moss_text": "MOSS 文生图",
    "text": "Qwen 文生图",
    "reference": "图片反推",
    "edit": "Qwen 单图／多图编辑",
    "oldx": "OLDX 人物替换",
    "character_card": "人物资产卡",
    "outpaint": "扩图",
    "remove": "遮罩移除",
    "upscale": "仅图片放大",
}
OLDX_MODES = ["oldx", "character_card"]
QWEN_LORA_DIR = "qwen2.1/"


def read(path):
    return json.loads(Path(path).read_text())


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def link(node, port=0):
    return [str(node), port]


def comfy_ref(value, graph):
    return (
        isinstance(value, list) and len(value) == 2
        and isinstance(value[0], str) and value[0] in graph and type(value[1]) is int
    )


def build(export, canvas):
    previous_graph = read(OLD / "qwen21-all-in-one.api.json")
    previous_mapping = read(OLD / "qwen21-all-in-one.mapping.json")["exposed_params"]
    fields = {field["key"]: deepcopy(field) for field in previous_mapping}
    graph = deepcopy(export)

    def inputs(node):
        return graph[str(node)]["inputs"]

    def put(node, cls, title, **values):
        graph[str(node)] = {"class_type": cls, "_meta": {"title": title}, "inputs": values}
        return link(node)

    def map_field(key, label, kind, node, arg, default=None, modes=None, **options):
        field = {"key": key, "label": label, "type": kind, "comfy_node_id": str(node),
                 "comfy_input": arg, "required": False, "default": default, **options}
        if modes:
            field["visible_when"] = {"input_mode": modes}
        fields[key] = field

    def condition(node, source, value, title):
        return put(node, "StringCompare", title, string_a=source, string_b=value, mode="Equal", case_sensitive=True)

    def switch(node, title, flag, yes, no=None):
        values = {"switch": flag, "on_true": yes}
        if no is not None:
            values["on_false"] = no
        return put(node, "ComfySwitchNode", title, **values)

    def qwen_lora(name):
        if name.startswith(("Qwen-功能2.1/", "Qwen/")):
            return QWEN_LORA_DIR + name.rsplit("/", 1)[1]
        return name

    def scalar_loras(node, title, clip, settings=None):
        original = inputs(node)
        slots = {name: original[name] for name in ("model", "clip") if name in original}
        slots.setdefault("clip", link(clip))
        for index in range(1, 5):
            preset = original.get(f"lora_{index}") or {}
            name = f"lora_{index:02d}"
            strength = f"strength_{index:02d}"
            slots[name] = preset.get("lora") if preset.get("on") else "None"
            slots[strength] = preset.get("strength", 1)
            if settings:
                slots[name] = settings[name]
                slots[strength] = settings[strength]
        put(node, "Lora Loader Stack (rgthree)", title, **slots)

    mode = put(700, "PrimitiveString", "功能模式", value="text")
    seed = put(701, "INTConstant", "种子", value=42)
    reverse = condition(702, mode, "reference", "图片反推")
    card = condition(703, mode, "character_card", "人物资产卡")
    engine = put(704, "PrimitiveString", "反推生图引擎", value="qwen")
    moss_reverse = condition(705, engine, "moss", "使用 MOSS 生图")
    edit_upscale = put(706, "PrimitiveBoolean", "编辑后高清放大", value=False)
    for node, arg in ((404, "seed"), (625, "noise_seed"), (190, "noise_seed"),
                      (273, "noise_seed"), (387, "seed")):
        inputs(node)[arg] = seed

    # Display nodes are canvas sinks. Downstream links consume their input instead.
    for display in (436, 441, 446, 536):
        for node in graph.values():
            for key, value in node["inputs"].items():
                if value == link(display):
                    node["inputs"][key] = inputs(display)["Any"]
    inputs(457)["switch"] = reverse
    inputs(458)["switch"] = reverse
    inputs(458)["on_true"] = reverse
    inputs(564)["value"] = ""
    inputs(393)["switch"] = False
    for node in (373, 392):
        inputs(node)["api_token"] = ""
        inputs(node)["api_url"] = previous_graph[str(node)]["inputs"]["api_url"]
        inputs(node)["prefer_local_config"] = True
    for node in (54, 127, 129, 137, 138, 141, 654):
        inputs(node)["image"] = "example.png"

    scalar_loras(166, "Qwen 编辑 LoRA", 471, previous_graph["166"]["inputs"])
    moss_loras = {index: inputs(397).get(f"lora_{index}") for index in range(1, 5)}
    scalar_loras(397, "MOSS 风格 LoRA", 396)
    scalar_loras(514, "OLDX 人物替换 LoRA", 471)
    for node in (166, 173, 274, 514):
        for name, value in inputs(node).items():
            if name.startswith("lora_") and isinstance(value, str):
                inputs(node)[name] = qwen_lora(value)
    for field in fields.values():
        if field["key"].startswith("lora_"):
            field["default"] = qwen_lora(field["default"])
            field["options"] = [qwen_lora(option) for option in field["options"]]
        if field["key"].startswith("strength_"):
            for key, options in field["visible_when"].items():
                if key.startswith("lora_"):
                    field["visible_when"][key] = [qwen_lora(option) for option in options]

    oldx_target = put(711, "LoadImage", "OLDX 姿势与场景参考", image="example.png")
    target_scaler = deepcopy(inputs(319))
    target_scaler.update({"图像": oldx_target, "遮罩": link(711, 1)})
    put(712, graph["319"]["class_type"], "OLDX 参考图预处理", **target_scaler)
    put(713, "LoadImage", "OLDX 补充参考图", image="example.png")
    # A required image input lets the bridge remove each unused scaler with its loader.
    for node, input_node in ((319, 127), (320, 129), (321, 141), (322, 138), (323, 137), (714, 713)):
        put(node, "ImageScaleToTotalPixels", "可选参考图预缩放", image=link(input_node),
            upscale_method="lanczos", megapixels=1.5, resolution_steps=1)
    oldx_extra_scaled = link(714)

    inputs(655).update(switch=card, on_false=link(712))
    inputs(661)["switch"] = card
    inputs(551)["on_false"] = link(661)
    inputs(537)["图像"] = link(519)
    inputs(534)["image_03"] = oldx_extra_scaled
    inputs(539)["images.image3"] = oldx_extra_scaled

    mask = put(280, "LoadImageMask", "黑白移除遮罩", image="example.png", channel="red")
    remove_source = deepcopy(inputs(318))
    remove_source["遮罩"] = mask
    remove_image = put(715, graph["318"]["class_type"], "移除原图与遮罩预处理", **remove_source)
    inputs(282).update({"图像": remove_image, "遮罩": link(715, 1)})
    inputs(179).update(gh_visual_layout="", gh_visual_ratio="自由比例")

    ref_output = switch(716, "反推输出", moss_reverse, link(406), link(14))
    selected = {
        "moss_text": link(406), "text": link(14), "reference": ref_output, "edit": link(69),
        "oldx": link(529), "character_card": link(529), "remove": link(268), "outpaint": link(191),
    }
    route = link(54)
    for index, (key, result) in enumerate(selected.items()):
        flag = condition(730 + index, mode, key, MODE_OPTIONS[key])
        route = switch(750 + index, MODE_OPTIONS[key], flag, result, route)
    inputs(383)["image"] = route
    upscale_only = condition(717, mode, "upscale", "仅图片放大")
    inputs(387)["image"] = switch(718, "放大原图或生成图", upscale_only, link(54), link(383))
    resolution = put(719, "INTConstant", "处理后放大短边", value=2560)
    inputs(387)["resolution"] = switch(720, "放大目标短边", upscale_only, link(391), resolution)
    scale_flag = switch(721, "是否放大", upscale_only, upscale_only, edit_upscale)
    put(389, "SaveImage", "最终输出", images=switch(722, "最终图像", scale_flag, link(387), route),
        filename_prefix="nous-qwen21-all-in-one")

    def target(key, node, arg=None):
        fields[key]["comfy_node_id"] = str(node)
        if arg is not None:
            fields[key]["comfy_input"] = arg

    target("input_mode", 700)
    fields["input_mode"]["options"] = [{"value": key, "label": label} for key, label in MODE_OPTIONS.items()]
    for key, node, arg in (("prompt", 564, "value"), ("enable_optimization", 456, "value"),
                           ("seed", 701, "value"), ("enable_upscale", 706, "value"),
                           ("edit_upscale_resolution", 719, "value")):
        target(key, node, arg)
    fields["prompt"]["visible_when"]["input_mode"] += ["moss_text", "remove", *OLDX_MODES]
    fields["prompt"]["required_when"]["input_mode"] += ["moss_text"]
    for field in ("image", "enable_optimization"):
        modes = fields[field]["visible_when"]["input_mode"]
        fields[field]["visible_when"]["input_mode"] = ([modes] if isinstance(modes, str) else modes) + OLDX_MODES
    fields["image"]["required_when"]["input_mode"] += OLDX_MODES
    for key in ("image", "mask"):
        fields[key].pop("omit_when_empty", None)
    fields["enable_optimization"]["visible_when"]["input_mode"] += ["moss_text"]
    fields["use_deepseek"].update(label="提示词优化模型", visible_when={
        "input_mode": ["edit", "text", "reference", "moss_text", *OLDX_MODES],
    })
    postprocess_modes = [key for key in MODE_OPTIONS if key != "upscale"]
    fields["enable_upscale"].update(label="处理后高清放大", visible_when={"input_mode": postprocess_modes})
    fields["edit_upscale_resolution"].update(label="放大短边（px）", visible_when={
        "input_mode": postprocess_modes, "enable_upscale": True,
    })
    maps = [
        ("reference_engine", "反推后使用的生图模型", "string", 704, "value", "qwen", "reference",
         {"options": [{"value": "qwen", "label": "Qwen Image 2.1"}, {"value": "moss", "label": "MOSS Image"}]}),
        ("moss_aspect_ratio", "MOSS 画幅", "string", 405, "aspect_ratio", inputs(405)["aspect_ratio"],
         ["moss_text", "reference"], {"options": ["1:1 (Square)", "2:3 (Portrait Photo)", "3:2 (Photo)",
             "3:4 (Portrait Standard)", "4:3 (Standard)", "9:16 (Portrait Widescreen)",
             "16:9 (Widescreen)", "21:9 (Ultrawide)"]}),
        ("moss_megapixels", "MOSS 生成像素（MP）", "number", 405, "megapixels", 2,
         ["moss_text", "reference"], {"min": 0.25, "max": 4, "step": 0.25}),
        ("moss_steps", "MOSS 采样步数", "integer", 404, "steps", inputs(404)["steps"], "moss_text",
         {"min": 1, "max": 60, "step": 1}),
        ("moss_cfg", "MOSS 提示词引导（CFG）", "number", 404, "cfg", inputs(404)["cfg"], "moss_text",
         {"min": 1, "max": 10, "step": 0.1}),
        ("oldx_target", "姿势与场景参考", "image", 711, "image", None, "oldx",
         {"required_when": {"input_mode": "oldx"}}),
        ("oldx_reference", "补充参考图", "image", 713, "image", None, OLDX_MODES, {"omit_when_empty": True}),
        ("oldx_megapixels", "人物编辑像素（MP）", "number", 537, "百万像素", 3, OLDX_MODES,
         {"min": 0.25, "max": 4, "step": 0.25}),
        ("oldx_cfg", "OLDX 提示词引导（CFG）", "number", 526, "cfg", inputs(526)["cfg"],
         OLDX_MODES, {"min": 1, "max": 10, "step": 0.1}),
        ("oldx_strength", "OLDX LoRA 强度", "number", 514, "strength_01", inputs(514)["strength_01"],
         OLDX_MODES, {"min": 0, "max": 2, "step": 0.05}),
    ]
    for key, label, kind, node, arg, default, modes, options in maps:
        map_field(key, label, kind, node, arg, default, modes, **options)

    moss_slots = {
        "KOOK-MOSS-SFT-观澜.safetensors": ("main", "观澜"),
        "feverdream-epoch-07.safetensors": ("dream", "FeverDream"),
        "TJFilmMossV2.safetensors": ("film", "TJFilmMossV2"),
    }
    for index, preset in moss_loras.items():
        if not preset or not preset.get("lora"):
            continue
        file = preset["lora"]
        slug, label = moss_slots.get(file.rsplit("/", 1)[-1], (f"slot_{index}", file.rsplit("/", 1)[-1]))
        key = f"moss_lora_{slug}"
        slot = f"lora_{index:02d}"
        strength = f"strength_{index:02d}"
        map_field(key, f"{index}. {label} LoRA", "string", 397, slot, inputs(397)[slot], "moss_text",
                  options=[{"value": file, "label": "启用"}, {"value": "None", "label": "关闭"}])
        map_field(f"{key}_strength", f"{label} 强度", "number", 397, strength, inputs(397)[strength], "moss_text",
                  min=0, max=2, step=0.05)
        fields[f"{key}_strength"]["visible_when"][key] = file

    active = {}

    def visit(node):
        if node in active:
            return
        active[node] = graph[node]
        for value in graph[node]["inputs"].values():
            if comfy_ref(value, graph):
                visit(value[0])

    visit("389")
    assert all(field["comfy_input"] in active[field["comfy_node_id"]]["inputs"] for field in fields.values())
    layout = {
        "nodes": [
            {"id": str(node["id"]), "type": node["type"], "pos": node["pos"][:2]}
            for node in canvas["nodes"] if str(node["id"]) in active
        ],
        "groups": [{"title": group["title"], "bounding": group["bounding"]} for group in canvas["groups"]],
    }
    return dict(sorted(active.items(), key=lambda item: int(item[0]))), {"exposed_params": list(fields.values())}, layout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", help="ComfyUI API-format export with every branch enabled")
    parser.add_argument("canvas", help="Original ComfyUI canvas JSON")
    args = parser.parse_args()
    graph, mapping, layout = build(read(args.export), read(args.canvas))
    dump(HERE / "workflow.api.json", graph)
    dump(HERE / "mapping.json", mapping)
    dump(HERE / "canvas.json", layout)


if __name__ == "__main__":
    main()
