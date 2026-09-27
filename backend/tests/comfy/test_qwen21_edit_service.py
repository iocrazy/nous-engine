"""nous-qwen21-image-edit:1 → 8 张参考图 + 桥的 `omit_when_empty` 剪枝。

模板与 mapping 直接读仓库里提交的部署产物 `docs/replications/qwen21-image-edit/`(官方前端
`app.graphToPrompt()` 转出的 API prompt + exposed_params)。sidecar 用替身,object_info 用
生产 ComfyUI `GET /object_info/{class}` 实测形状的摘录,绝不碰真 ComfyUI。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import src.services.nodes.comfy_bridge as nb
from src.services.nodes.registry import get_node_class

ARTIFACTS = Path(__file__).resolve().parents[3] / "docs" / "replications" / "qwen21-image-edit"
SERVICE = "nous-qwen21-image-edit"
ENCODER = "497"
IMAGE_KEYS = ["image"] + [f"image{k}" for k in range(2, 9)]

PNG1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==")
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG1PX).decode()

# 生产 ComfyUI(:8888)`GET /object_info/{class}` 的摘录(只留 input / output_node)。
OBJECT_INFO = {
    "LoadImage": {"input": {"required": {"image": [[], {"image_upload": True}]}},
                  "output_node": False},
    "ImageScaleToTotalPixels": {"input": {"required": {
        "image": ["IMAGE", {}], "upscale_method": ["COMBO", {}], "megapixels": ["FLOAT", {}],
        "resolution_steps": ["INT", {}]}}, "output_node": False},
    "TextEncodeQwenImage21": {"input": {
        "required": {"clip": ["CLIP", {}], "prompt": ["STRING", {}],
                     "negative_prompt": ["STRING", {}], "resolution": ["INT", {}],
                     "images": ["COMFY_AUTOGROW_V3", {}]},
        "optional": {"vae": ["VAE", {}]}}, "output_node": False},
    "Image Comparer (rgthree)": {"input": {"required": {}, "optional": {
        "image_a": ["IMAGE"], "image_b": ["IMAGE"]}}, "output_node": True},
    "VAEDecode": {"input": {"required": {"samples": ["LATENT", {}], "vae": ["VAE", {}]}},
                  "output_node": False},
    "SaveImage": {"input": {"required": {"images": ["IMAGE", {}],
                                         "filename_prefix": ["STRING", {}]}},
                  "output_node": True},
}


def _artifact() -> tuple[dict, dict]:
    wf = json.loads((ARTIFACTS / "qwen21-image-edit.api.json").read_text())
    mapping = json.loads((ARTIFACTS / "qwen21-image-edit.mapping.json").read_text())
    return wf, mapping


def _placeholder_files(wf: dict) -> set[str]:
    return {n["inputs"]["image"] for n in wf.values() if n["class_type"] == "LoadImage"}


# ---------- 产物本身 ----------


def test_mapping_has_eight_images_first_required_rest_omittable():
    wf, mapping = _artifact()
    params = {p["key"]: p for p in mapping["exposed_params"]}
    assert [k for k in params if params[k]["type"] == "image"] == IMAGE_KEYS
    assert params["image"]["required"] is True
    assert not params["image"].get("omit_when_empty")
    for k in IMAGE_KEYS[1:]:
        assert params[k]["required"] is False
        assert params[k]["omit_when_empty"] is True
        assert params[k].get("default") is None
    # 老参数名与语义不变
    assert {"prompt", "aspect_ratio", "megapixels", "seed"} <= set(params)
    assert params["prompt"]["comfy_node_id"] == "571"
    assert params["image"]["comfy_node_id"] == "475"


def test_every_mapping_target_exists_in_workflow():
    wf, mapping = _artifact()
    for p in mapping["exposed_params"]:
        node = wf.get(p["comfy_node_id"])
        assert node is not None, p["key"]
        assert p["comfy_input"] in node["inputs"], p["key"]


def test_image_k_feeds_encoder_slot_k():
    """image{k} 的 LoadImage 经一个 ImageScaleToTotalPixels 接到编码器的 images.image_k。"""
    wf, mapping = _artifact()
    params = {p["key"]: p for p in mapping["exposed_params"]}
    enc = wf[ENCODER]["inputs"]
    for k, key in enumerate(IMAGE_KEYS, start=1):
        scale_id = enc[f"images.image_{k}"][0]
        assert wf[scale_id]["class_type"] == "ImageScaleToTotalPixels"
        assert wf[scale_id]["inputs"]["image"][0] == params[key]["comfy_node_id"]


def test_workflow_has_no_prompt_enhancer_or_generate_branch():
    wf, _ = _artifact()
    classes = {n["class_type"] for n in wf.values()}
    assert "TE_Qwen_Image_2_1_Prompt_Enhancer" not in classes
    assert sum(1 for n in wf.values() if n["class_type"] == "KSampler") == 1
    assert sum(1 for n in wf.values() if n["class_type"] == "LoadImage") == 8


# ---------- 桥:按传入张数剪枝 ----------


class _Fake:
    def __init__(self):
        self.uploaded: list[str] = []
        self.submitted: dict | None = None

    async def upload_image(self, filename, content, mime="image/png"):
        self.uploaded.append(filename)
        return f"up_{filename}"

    async def submit(self, graph):
        self.submitted = graph
        return "p1"

    async def wait(self, prompt_id, *, timeout_s, interval_s=2.0, **_kw):
        return {"outputs": {"487": {"images": [
            {"filename": "QW21_00001_.png", "subfolder": "", "type": "output"}]}}}

    async def download(self, item):
        return b"PNG"


@pytest.fixture
def bridge(monkeypatch):
    wf, mapping = _artifact()
    fake = _Fake()
    info_calls: list[list[str]] = []
    state = {"infos": OBJECT_INFO}

    async def fake_load_template(_tid):
        return wf, mapping["exposed_params"]

    async def fake_infos(class_types):
        cts = list(class_types)
        info_calls.append(cts)
        return {ct: state["infos"].get(ct) for ct in cts}

    async def fake_write(data, *, ext, ttl_seconds=86400):
        return {"url": f"/files/x.{ext}", "uuid": "u", "ext": ext}

    monkeypatch.setattr(nb, "get_client", lambda: fake)
    monkeypatch.setattr(nb, "load_template", fake_load_template)
    monkeypatch.setattr(nb, "get_node_infos", fake_infos)
    monkeypatch.setattr(nb, "write_media", fake_write)
    return {"fake": fake, "wf": wf, "info_calls": info_calls, "state": state}


async def _run(keys: list[str]) -> None:
    node = get_node_class("comfyui_workflow")()
    data = {"template_id": 1, "prompt": "让图一的人物穿上图二的衣服"}
    data.update({k: PNG_URI for k in keys})
    out = await node.invoke(data, {})
    assert out["image_url"] == "/files/x.png"


def _slots(graph: dict) -> list[int]:
    return sorted(int(k.rsplit("_", 1)[-1]) for k in graph[ENCODER]["inputs"]
                  if k.startswith("images.image_"))


def _load_images(graph: dict) -> list[str]:
    return [n["inputs"]["image"] for n in graph.values() if n["class_type"] == "LoadImage"]


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [1, 3, 8])
async def test_n_images_yield_n_slots_and_n_loaders(bridge, n):
    await _run(IMAGE_KEYS[:n])
    g = bridge["fake"].submitted
    assert _slots(g) == list(range(1, n + 1))
    loads = _load_images(g)
    assert len(loads) == n
    assert all(f.startswith("up_") for f in loads)  # 全是本次上传,没有占位图
    assert not (set(loads) & _placeholder_files(bridge["wf"]))
    assert len(bridge["fake"].uploaded) == n
    scalers = [nid for nid, nd in g.items() if nd["class_type"] == "ImageScaleToTotalPixels"]
    assert len(scalers) == n
    # 主链与输出节点都在
    assert {"487", "480", "481", ENCODER, "530"} <= set(g)


@pytest.mark.asyncio
async def test_one_image_does_not_query_object_info_for_unrelated_nodes(bridge):
    await _run(["image"])
    queried = set(bridge["info_calls"][0])
    # 只问被剪 LoadImage 的下游(缩放 + 编码器 + 其后链路),不问加载器之类
    assert "ImageScaleToTotalPixels" in queried and "TextEncodeQwenImage21" in queried
    assert "UNETLoader" not in queried and "LoadImage" not in queried


@pytest.mark.asyncio
async def test_all_images_supplied_skips_object_info(bridge):
    await _run(IMAGE_KEYS)
    assert bridge["info_calls"] == []


@pytest.mark.asyncio
async def test_gap_in_middle_keeps_supplied_slots(bridge):
    """传 1、3 不传 2 → 编码器只剩 image_1 与 image_3(ComfyUI 端按序号排序后依次喂入)。"""
    await _run(["image", "image3"])
    g = bridge["fake"].submitted
    assert _slots(g) == [1, 3]
    assert len(_load_images(g)) == 2


@pytest.mark.asyncio
async def test_object_info_unreachable_still_prunes_conservatively(bridge):
    """sidecar 的 object_info 全取不到 → 缩放节点保守删、编码器 autogrow 子键照删,照样出图。"""
    bridge["state"]["infos"] = {}
    await _run(["image", "image2"])
    g = bridge["fake"].submitted
    assert _slots(g) == [1, 2]
    assert len(_load_images(g)) == 2


@pytest.mark.asyncio
async def test_empty_string_counts_as_not_supplied(bridge):
    node = get_node_class("comfyui_workflow")()
    await node.invoke({"template_id": 1, "prompt": "p", "image": PNG_URI, "image2": ""}, {})
    assert _slots(bridge["fake"].submitted) == [1]


@pytest.mark.asyncio
async def test_omit_flag_on_output_critical_param_raises(monkeypatch, bridge):
    """mapping 配错(把主图标成可省略,且它一路 required 到 SaveImage)→ 清晰报错,不提交。"""
    wf = json.loads(json.dumps(bridge["wf"]))
    wf["480"]["inputs"]["samples"] = ["532", 0]  # 让主图支路经 required 输入直达 SaveImage
    _, mapping = _artifact()
    params = [dict(p) for p in mapping["exposed_params"]]
    for p in params:
        if p["key"] == "image":
            p.update(required=False, omit_when_empty=True)

    async def fake_load_template(_tid):
        return wf, params
    monkeypatch.setattr(nb, "load_template", fake_load_template)
    node = get_node_class("comfyui_workflow")()
    # 顺带传两张别的图:剪枝报错必须发生在任何上传之前,不留孤儿文件
    with pytest.raises(ValueError, match="omit_when_empty"):
        await node.invoke({"template_id": 1, "prompt": "p",
                           "image2": PNG_URI, "image3": PNG_URI}, {})
    assert bridge["fake"].submitted is None
    assert bridge["fake"].uploaded == []


# ---------- 控制面:mapping 持久化 + schema ----------


@pytest.mark.asyncio
async def test_mapping_roundtrip_keeps_omit_flag_and_schema_optional(client):
    wf, mapping = _artifact()
    r = await client.post("/api/v1/comfy-templates",
                          json={"name": SERVICE, "workflow": wf, "output_kind": "image"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)
    assert r.status_code == 200, r.text
    detail = (await client.get(f"/api/v1/comfy-templates/{tid}")).json()
    got = {p["key"]: p for p in detail["exposed_params"]}
    assert got["image"]["omit_when_empty"] is False
    for k in IMAGE_KEYS[1:]:
        assert got[k]["omit_when_empty"] is True
    schema = (await client.get(f"/v1/services/{SERVICE}/schema")).json()["input_schema"]
    assert "image" in schema["required"]
    assert not set(IMAGE_KEYS[1:]) & set(schema.get("required") or [])
    assert set(IMAGE_KEYS) <= set(schema["properties"])


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"type": "string", "required": False},   # 非文件类
    {"type": "image", "required": True},     # 必填又可省略,自相矛盾
    # 桥的上传类型集合里没有 binary,大小写也按桥的原样比对 —— 存得进去就会静默不剪
    {"type": "binary", "required": False},
    {"type": "Image", "required": False},
    # 有 default 就永远不算「未传」,标了也不会剪(编辑器曾把占位文件名写进 default)
    {"type": "image", "required": False, "default": "5 (1).jpg"},
])
async def test_mapping_rejects_invalid_omit_flag(client, bad):
    wf, _ = _artifact()
    r = await client.post("/api/v1/comfy-templates", json={"name": "nous-omit-bad", "workflow": wf})
    tid = r.json()["id"]
    body = {"exposed_params": [{"key": "x", "comfy_node_id": "505", "comfy_input": "image",
                                "omit_when_empty": True, **bad}]}
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=body)
    assert r.status_code == 400, r.text
    assert "omit_when_empty" in r.text
