"""nous-qwen21-text-to-image:Qwen Image 2.1 文生图一采 + 固定 8 槽 LoRA。

模板与 mapping 直接读仓库里提交的部署产物 `docs/replications/qwen21-text-to-image/`。
8 个槽由两个串联的 `Lora Loader Stack (rgthree)`(各 4 槽)组成:lora_1..4 → 700,
lora_5..8 → 701。sidecar 用替身,绝不碰真 ComfyUI(:8888)。
"""
from __future__ import annotations

import asyncio
import json
import secrets as _secrets
from pathlib import Path

import bcrypt
import pytest
from sqlalchemy import select

import src.api.routes.comfy_templates as comfy_templates_route
import src.services.nodes.comfy_bridge as nb
from src.models.api_gateway import ApiKeyGrant
from src.models.database import get_session_factory
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance

ARTIFACTS = Path(__file__).resolve().parents[3] / "docs" / "replications" / "qwen21-text-to-image"
SERVICE = "nous-qwen21-text-to-image"
STACK = "Lora Loader Stack (rgthree)"
STACK_A, STACK_B = "700", "701"
QWEN_LORA = "Qwen/Qwen-Image-2.1-viggle-turbo-4step-lora-r64.safetensors"
LORA_KEYS = [f"lora_{i}" for i in range(1, 9)]

# 调用方绝不能改到的模型/权重输入(class_type → input 名)。
MODEL_INPUTS = {
    "UNETLoader": {"unet_name", "weight_dtype"},
    "CLIPLoader": {"clip_name", "type", "device"},
    "VAELoader": {"vae_name"},
}


def _artifact() -> tuple[dict, dict]:
    wf = json.loads((ARTIFACTS / "qwen21-text-to-image.api.json").read_text())
    mapping = json.loads((ARTIFACTS / "qwen21-text-to-image.mapping.json").read_text())
    return wf, mapping


def _slot_target(i: int) -> tuple[str, str]:
    """lora_i → (Stack 节点, 该节点上的槽号 01..04)。"""
    return (STACK_A if i <= 4 else STACK_B), f"{(i - 1) % 4 + 1:02d}"


# ---------- 产物本身 ----------


def test_workflow_is_the_bare_t2i_branch():
    """只有文生图主链 + 两个 Stack:没有编辑分支、预览、PE、标签、对比器、bypass 开关。"""
    wf, _ = _artifact()
    classes = sorted(n["class_type"] for n in wf.values())
    assert classes == sorted([
        "SaveImage", "ResolutionSelector", "EmptyLatentImage", "KSampler",
        "PrimitiveStringMultiline", "CLIPLoader", "VAELoader", "VAEDecode",
        "TextEncodeQwenImage21", "UNETLoader", "Seed (rgthree)", STACK, STACK])
    assert all("mode" not in n for n in wf.values())  # API prompt 里没有 bypass 残留


def test_lora_stacks_are_chained_between_loaders_and_consumers():
    wf, _ = _artifact()
    a, b = wf[STACK_A]["inputs"], wf[STACK_B]["inputs"]
    assert (a["model"], a["clip"]) == (["523", 0], ["518", 0])  # UNETLoader / CLIPLoader
    assert (b["model"], b["clip"]) == ([STACK_A, 0], [STACK_A, 1])
    assert wf["515"]["inputs"]["model"] == [STACK_B, 0]  # KSampler 吃第二个 Stack 的 MODEL
    assert wf["522"]["inputs"]["clip"] == [STACK_B, 1]   # 编码器吃第二个 Stack 的 CLIP
    for node in (a, b):
        for s in ("01", "02", "03", "04"):
            assert node[f"lora_{s}"] == "None"
            assert node[f"strength_{s}"] == 1


def test_model_weights_are_baked_in():
    wf, _ = _artifact()
    by_class = {n["class_type"]: n["inputs"] for n in wf.values()}
    assert by_class["UNETLoader"]["unet_name"] == "Qwen/qwen_image_2.1_bf16.safetensors"
    assert by_class["CLIPLoader"]["clip_name"] == "qwen3vl_8b_bf16.safetensors"
    assert by_class["VAELoader"]["vae_name"] == "qwen_image_2.1_vae_bf16.safetensors"


def test_mapping_targets_real_nodes_and_never_loaders():
    wf, mapping = _artifact()
    for p in mapping["exposed_params"]:
        node = wf.get(p["comfy_node_id"])
        assert node is not None, p["key"]
        assert p["comfy_input"] in node["inputs"], p["key"]
        assert p["comfy_input"] not in MODEL_INPUTS.get(node["class_type"], set()), p["key"]


def test_mapping_public_keys_and_legacy_targets():
    _, mapping = _artifact()
    params = {p["key"]: p for p in mapping["exposed_params"]}
    base = ["prompt", "aspect_ratio", "megapixels", "seed"]
    lora = [k for i in range(1, 9) for k in (f"lora_{i}", f"lora_{i}_strength")]
    assert list(params) == base + lora  # 顺序稳定:1 → 8
    # 与生产现有模板一致的老映射
    assert (params["prompt"]["comfy_node_id"], params["prompt"]["comfy_input"]) == ("517", "value")
    assert params["prompt"]["required"] is True
    assert params["seed"]["comfy_node_id"] == "524" and params["seed"]["random"] is True
    assert {params[k]["comfy_node_id"] for k in ("aspect_ratio", "megapixels")} == {"512"}
    assert not [p for p in params.values() if p["type"] in ("image", "file", "video")]


def test_mapping_eight_lora_slots_in_order():
    _, mapping = _artifact()
    params = {p["key"]: p for p in mapping["exposed_params"]}
    for i in range(1, 9):
        node, slot = _slot_target(i)
        lora, strength = params[f"lora_{i}"], params[f"lora_{i}_strength"]
        assert (lora["comfy_node_id"], lora["comfy_input"]) == (node, f"lora_{slot}")
        assert (strength["comfy_node_id"], strength["comfy_input"]) == (node, f"strength_{slot}")
        assert lora["default"] == "None" and lora["required"] is False
        assert [o["value"] for o in lora["options"]] == ["None", QWEN_LORA]
        # strength 用节点真实约束(object_info:FLOAT default 1.0 min -10 max 10 step 0.01)
        assert (strength["min"], strength["max"], strength["step"], strength["default"]) \
            == (-10.0, 10.0, 0.01, 1.0)
    assert mapping["discovery"] == {
        "operation": "text_to_image", "display_name": "Qwen Image 2.1 文生图", "lora_slots": 8}


def test_lora_options_are_relative_names_only():
    _, mapping = _artifact()
    for p in mapping["exposed_params"]:
        if p["key"] not in LORA_KEYS:
            continue
        for o in p["options"]:
            v = o["value"]
            assert not v.startswith("/") and ".." not in v and "\\" not in v and ":" not in v


# ---------- 部署 + 调用 ----------


class T2IFakeClient:
    def __init__(self, gate: asyncio.Event | None = None):
        self.submitted: dict | None = None
        self.uploaded: list[str] = []
        self.gate = gate
        self.interrupt_calls = 0

    async def upload_image(self, filename, content, mime="image/png"):
        self.uploaded.append(filename)
        return f"up_{filename}"

    async def submit(self, graph):
        self.submitted = graph
        return "p1"

    async def wait(self, prompt_id, *, timeout_s, interval_s=2.0, **_kw):
        if self.gate is not None:
            await self.gate.wait()
        return {"outputs": {"508": {"images": [
                    {"filename": "QW21_00001_.png", "subfolder": "", "type": "output"}]}},
                "status": {"status_str": "success", "completed": True, "messages": []}}

    async def download(self, item):
        return b"PNGDATA"

    async def interrupt(self) -> None:
        self.interrupt_calls += 1


def _use(monkeypatch, fc: T2IFakeClient) -> T2IFakeClient:
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    monkeypatch.setattr(comfy_templates_route, "get_client", lambda: fc)
    return fc


async def _deploy(client) -> str:
    wf, mapping = _artifact()
    r = await client.post("/api/v1/comfy-templates", json={"name": SERVICE, "workflow": wf})
    assert r.status_code == 201, r.text
    assert r.json()["output_kind"] == "image"
    tid = r.json()["id"]
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)
    assert r.status_code == 200, r.text
    return tid


async def _mint_key() -> str:
    raw = f"sk-t2i-{_secrets.token_hex(8)}"
    async with get_session_factory()() as s:
        key = InstanceApiKey(
            instance_id=None, label="t",
            key_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
            key_prefix=raw[:10], is_active=True)
        s.add(key)
        await s.commit()
        await s.refresh(key)
        svc = (await s.execute(
            select(ServiceInstance).where(ServiceInstance.name == SERVICE))).scalar_one()
        s.add(ApiKeyGrant(api_key_id=key.id, service_id=svc.id, status="active"))
        await s.commit()
    return raw


async def _predict(client, inputs: dict, headers: dict | None = None):
    return await client.post(
        f"/v1/services/{SERVICE}/predictions", json={"input": inputs}, headers=headers or {})


def _stack_values(g: dict) -> list[tuple[str, float]]:
    out = []
    for i in range(1, 9):
        node, slot = _slot_target(i)
        out.append((g[node]["inputs"][f"lora_{slot}"], g[node]["inputs"][f"strength_{slot}"]))
    return out


@pytest.mark.asyncio
async def test_schema(client):
    await _deploy(client)
    body = (await client.get(f"/v1/services/{SERVICE}/schema")).json()
    ins = body["input_schema"]
    assert ins["required"] == ["prompt"]
    assert list(ins["properties"])[:4] == ["prompt", "aspect_ratio", "megapixels", "seed"]
    assert ins["properties"]["lora_1"]["enum"] == ["None", QWEN_LORA]
    assert ins["properties"]["lora_1"]["default"] == "None"
    assert ins["properties"]["lora_1"]["x-option-meta"][1]["label"] == "Viggle Turbo 4 步加速"
    assert ins["properties"]["lora_8_strength"] == {
        "type": "number", "minimum": -10.0, "maximum": 10.0, "default": 1.0,
        "description": "LoRA 8 强度"}
    assert list(body["output_schema"]["properties"]) == ["image_url"]


@pytest.mark.asyncio
async def test_sync_prediction_returns_image_url_and_defaults_leave_slots_empty(
        client, monkeypatch):
    fc = _use(monkeypatch, T2IFakeClient())
    await _deploy(client)
    r = await _predict(client, {"prompt": "一只橘猫"})
    pred = r.json()
    assert pred["status"] == "succeeded", pred
    out = pred["output"]["outputs"]["out"]
    assert out["image_url"].split("?")[0].endswith(".png")
    g = fc.submitted
    assert g["517"]["inputs"]["value"] == "一只橘猫"
    # 未传 LoRA:8 个槽全是 "None"(rgthree 对 None 槽不加载任何 LoRA)
    assert _stack_values(g) == [("None", 1.0)] * 8
    assert fc.uploaded == []


@pytest.mark.asyncio
async def test_lora_slots_keep_order_and_none_slots_stay_empty(client, monkeypatch):
    fc = _use(monkeypatch, T2IFakeClient())
    await _deploy(client)
    inputs = {"prompt": "p",
              "lora_1": QWEN_LORA, "lora_1_strength": 0.8,
              "lora_3": "None", "lora_3_strength": 0.3,
              "lora_5": QWEN_LORA, "lora_5_strength": -1.5,
              "lora_8": QWEN_LORA, "lora_8_strength": 10}
    r = await _predict(client, inputs)
    assert r.json()["status"] == "succeeded", r.text
    assert _stack_values(fc.submitted) == [
        (QWEN_LORA, 0.8), ("None", 1.0), ("None", 0.3), ("None", 1.0),
        (QWEN_LORA, -1.5), ("None", 1.0), ("None", 1.0), (QWEN_LORA, 10)]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"lora_1": "krea2/krea2-Cc风情万种-人像.safetensors"},  # ComfyUI 上有,但不是 Qwen 2.1 的
    {"lora_2": "/media/heygo/models/evil.safetensors"},
    {"lora_3": "../../etc/passwd"},
    {"lora_4": "Qwen\\Qwen-Image-2.1-viggle-turbo-4step-lora-r64.safetensors"},
    {"lora_5": ""},
    {"lora_6": 1},
    {"lora_7_strength": 10.01},
    {"lora_8_strength": -11},
    {"lora_1_strength": "1"},
])
async def test_incompatible_or_illegal_lora_rejected_before_render(client, monkeypatch, bad):
    fc = _use(monkeypatch, T2IFakeClient())
    await _deploy(client)
    r = await _predict(client, {"prompt": "p", **bad})
    assert r.status_code == 422, r.text
    assert fc.submitted is None


@pytest.mark.asyncio
async def test_model_loader_inputs_cannot_be_overridden(client, monkeypatch):
    fc = _use(monkeypatch, T2IFakeClient())
    await _deploy(client)
    evil = {"unet_name": "../../x.safetensors", "clip_name": "/tmp/evil", "vae_name": "x",
            "lora_01": "/abs.safetensors", "strength_01": 99, "model": ["523", 0],
            "filename_prefix": "../../../tmp/pwn", "template_id": 1}
    r = await _predict(client, {"prompt": "p", **evil})
    assert r.json()["status"] == "succeeded", r.text
    wf, _ = _artifact()
    g = fc.submitted
    for nid, node in g.items():
        for inp in MODEL_INPUTS.get(node["class_type"], ()):
            assert node["inputs"][inp] == wf[nid]["inputs"][inp]
    assert g["508"]["inputs"]["filename_prefix"] == wf["508"]["inputs"]["filename_prefix"]
    assert _stack_values(g) == [("None", 1.0)] * 8


@pytest.mark.asyncio
async def test_missing_prompt_rejected(client, monkeypatch):
    fc = _use(monkeypatch, T2IFakeClient())
    await _deploy(client)
    r = await _predict(client, {"lora_1": QWEN_LORA})
    assert r.status_code == 422, r.text
    assert fc.submitted is None


# ---------- 异步 + 轮询 + 取消 ----------


async def _poll(client, pid, headers, until=("succeeded", "failed", "canceled"),
                timeout: float = 5.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    body: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/predictions/{pid}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in until:
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"prediction {pid} 未到 {until}:{body}")


@pytest.mark.asyncio
async def test_async_prediction_and_polling(client, monkeypatch):
    fc = _use(monkeypatch, T2IFakeClient())
    await _deploy(client)
    headers = {"Authorization": f"Bearer {await _mint_key()}"}
    r = await _predict(client, {"prompt": "p", "lora_2": QWEN_LORA},
                       headers={**headers, "Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    final = await _poll(client, r.json()["id"], headers)
    assert final["status"] == "succeeded", final
    assert final["output"]["outputs"]["out"]["image_url"]
    assert _stack_values(fc.submitted)[1] == (QWEN_LORA, 1.0)


@pytest.mark.asyncio
async def test_cancel_running_prediction(client, monkeypatch):
    gate = asyncio.Event()
    fc = _use(monkeypatch, T2IFakeClient(gate))
    await _deploy(client)
    headers = {"Authorization": f"Bearer {await _mint_key()}"}
    r = await _predict(client, {"prompt": "p"}, headers={**headers, "Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    pid = r.json()["id"]
    await _poll(client, pid, headers, until=("processing",))
    deadline = asyncio.get_event_loop().time() + 5.0
    while nb.get_running_task_id() != int(pid):  # 进入渲染信号量,cancel 才会转发 interrupt
        assert asyncio.get_event_loop().time() < deadline, "渲染未开始"
        await asyncio.sleep(0.01)

    c = await client.post(f"/v1/predictions/{pid}/cancel", headers=headers)
    assert c.status_code == 200, c.text
    assert c.json()["status"] == "canceled"
    assert fc.interrupt_calls == 1
    gate.set()
    final = await _poll(client, pid, headers)
    assert final["status"] == "canceled" and final["output"] is None
