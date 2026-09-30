"""服务发现元数据(`discovery`)+ 公开目录 `GET /v1/services`。

调用方(nous-app)不认服务名也能区分文生图 / 图像编辑 / 图片放大:mapping 顶层的
`discovery = {operation, display_name, lora_slots}` 随 `PUT /{id}/mapping` 落库;
`supports_lora` / `media_inputs` / `prompt_fields` 由 exposed_inputs 推导,不重复存。
工作流参数的唯一真相仍是 `/v1/services/{name}/schema` 的 input_schema。
"""
from __future__ import annotations

import json
import secrets as _secrets
from pathlib import Path

import bcrypt
import pytest
from sqlalchemy import select

from src.models.api_gateway import ApiKeyGrant
from src.models.database import get_session_factory
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance

REPL = Path(__file__).resolve().parents[3] / "docs" / "replications"

# service → (产物目录, 产物前缀)
ATOMIC = {
    "nous-qwen21-text-to-image": ("qwen21-text-to-image", "qwen21-text-to-image"),
    "nous-qwen21-image-edit": ("qwen21-image-edit", "qwen21-image-edit"),
    "nous-seedvr2-image-upscale": ("upscale", "seedvr2-image-upscale"),
    "nous-vosr2-image-upscale": ("upscale", "vosr2-image-upscale"),
}
EDIT_IMAGES = ["image"] + [f"image{k}" for k in range(2, 9)]

EXPECTED = {
    "nous-qwen21-text-to-image": {
        "operation": "text_to_image", "display_name": "Qwen Image 2.1 文生图",
        "supports_lora": True, "lora_slots": 8, "media_inputs": [], "prompt_fields": ["prompt"]},
    "nous-qwen21-image-edit": {
        "operation": "image_edit", "display_name": "Qwen Image 2.1 图像编辑",
        "supports_lora": False, "lora_slots": 0,
        "media_inputs": [{"key": k, "type": "image", "required": k == "image"}
                         for k in EDIT_IMAGES],
        "prompt_fields": ["prompt"]},
    "nous-seedvr2-image-upscale": {
        "operation": "image_upscale", "display_name": "SeedVR2",
        "supports_lora": False, "lora_slots": 0,
        "media_inputs": [{"key": "image", "type": "image", "required": True}],
        "prompt_fields": []},
    "nous-vosr2-image-upscale": {
        "operation": "image_upscale", "display_name": "VOSR2",
        "supports_lora": False, "lora_slots": 0,
        "media_inputs": [{"key": "image", "type": "image", "required": True}],
        "prompt_fields": []},
}


def _artifact(service: str) -> tuple[dict, dict]:
    d, prefix = ATOMIC[service]
    wf = json.loads((REPL / d / f"{prefix}.api.json").read_text())
    mapping = json.loads((REPL / d / f"{prefix}.mapping.json").read_text())
    return wf, mapping


async def _deploy(client, service: str, mapping: dict | None = None) -> str:
    wf, repo_mapping = _artifact(service)
    r = await client.post("/api/v1/comfy-templates", json={"name": service, "workflow": wf})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping or repo_mapping)
    assert r.status_code == 200, r.text
    return tid


async def _mint_key(*services: str) -> str:
    raw = f"sk-disc-{_secrets.token_hex(8)}"
    async with get_session_factory()() as s:
        key = InstanceApiKey(
            instance_id=None, label="t",
            key_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
            key_prefix=raw[:10], is_active=True)
        s.add(key)
        await s.commit()
        await s.refresh(key)
        for name in services:
            svc = (await s.execute(
                select(ServiceInstance).where(ServiceInstance.name == name))).scalar_one()
            s.add(ApiKeyGrant(api_key_id=key.id, service_id=svc.id, status="active"))
        await s.commit()
    return raw


# ---------- schema 端点带 discovery ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(ATOMIC))
async def test_schema_carries_discovery(client, service):
    await _deploy(client, service)
    body = (await client.get(f"/v1/services/{service}/schema")).json()
    assert body["discovery"] == EXPECTED[service]
    assert list(body["output_schema"]["properties"]) == ["image_url"]
    # 发现元数据不泄露 ComfyUI 内部定位
    blob = json.dumps(body["discovery"])
    assert "comfy_node_id" not in blob and "8888" not in blob and "/media/" not in blob


@pytest.mark.asyncio
async def test_schema_discovery_null_without_metadata(client):
    wf, mapping = _artifact("nous-vosr2-image-upscale")
    mapping = {"exposed_params": mapping["exposed_params"]}
    await _deploy(client, "nous-vosr2-image-upscale", mapping)
    body = (await client.get("/v1/services/nous-vosr2-image-upscale/schema")).json()
    assert body["discovery"] is None


@pytest.mark.asyncio
async def test_template_detail_roundtrips_discovery(client):
    tid = await _deploy(client, "nous-qwen21-text-to-image")
    detail = (await client.get(f"/api/v1/comfy-templates/{tid}")).json()
    assert detail["discovery"] == {
        "operation": "text_to_image", "display_name": "Qwen Image 2.1 文生图", "lora_slots": 8}


# ---------- 写入语义 ----------


@pytest.mark.asyncio
async def test_mapping_without_discovery_keeps_stored_one(client):
    """管理后台编辑器只发 exposed_params —— 不能因此把 discovery 冲掉。"""
    tid = await _deploy(client, "nous-seedvr2-image-upscale")
    _, mapping = _artifact("nous-seedvr2-image-upscale")
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                         json={"exposed_params": mapping["exposed_params"]})
    assert r.status_code == 200, r.text
    body = (await client.get("/v1/services/nous-seedvr2-image-upscale/schema")).json()
    assert body["discovery"]["operation"] == "image_upscale"


@pytest.mark.asyncio
async def test_explicit_null_clears_discovery(client):
    tid = await _deploy(client, "nous-seedvr2-image-upscale")
    _, mapping = _artifact("nous-seedvr2-image-upscale")
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                         json={"discovery": None, "exposed_params": mapping["exposed_params"]})
    assert r.status_code == 200, r.text
    body = (await client.get("/v1/services/nous-seedvr2-image-upscale/schema")).json()
    assert body["discovery"] is None
    async with get_session_factory()() as s:  # 清除 = SQL NULL(不是 JSON 'null'),目录查询靠它
        n = (await s.execute(select(ServiceInstance.id).where(
            ServiceInstance.name == "nous-seedvr2-image-upscale",
            ServiceInstance.discovery.is_(None)))).all()
    assert len(n) == 1


@pytest.mark.asyncio
async def test_mapping_that_breaks_stored_lora_slots_is_rejected(client):
    """不带 discovery 的新 mapping 删掉了 LoRA 槽 → 已存的 lora_slots=8 会撒谎,拒。"""
    tid = await _deploy(client, "nous-qwen21-text-to-image")
    _, mapping = _artifact("nous-qwen21-text-to-image")
    params = [p for p in mapping["exposed_params"] if not p["key"].startswith("lora_8")]
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                         json={"exposed_params": params})
    assert r.status_code == 400, r.text
    assert "lora_8" in r.text


def _t2i_with(mutate) -> dict:
    _, mapping = _artifact("nous-qwen21-text-to-image")
    mapping = json.loads(json.dumps(mapping))
    mutate(mapping)
    return mapping


def _param(mapping: dict, key: str) -> dict:
    return next(p for p in mapping["exposed_params"] if p["key"] == key)


@pytest.mark.asyncio
@pytest.mark.parametrize(("mutate", "needle"), [
    (lambda m: m["discovery"].update(operation="video_magic"), "operation"),
    (lambda m: m["discovery"].update(display_name="  "), "display_name"),
    (lambda m: m["discovery"].update(lora_slots=9), "lora_9"),
    (lambda m: m["discovery"].update(lora_slots=-1), "lora_slots"),
    (lambda m: m["discovery"].update(lora_slots=4), "lora_5"),  # 多出来的槽没声明
    (lambda m: _param(m, "lora_2").pop("options"), "lora_2"),   # 自由路径
    (lambda m: _param(m, "lora_3").update(
        options=["None", "/media/heygo/models/loras/x.safetensors"]), "lora_3"),
    (lambda m: _param(m, "lora_4").update(options=["None", "../x.safetensors"]), "lora_4"),
    (lambda m: _param(m, "lora_5").update(options=["None", "C:\\x.safetensors"]), "lora_5"),
    (lambda m: _param(m, "lora_6").update(options=["a.safetensors"], default="a.safetensors"),
     "None"),
    (lambda m: _param(m, "lora_7").update(default="zzz.safetensors"), "lora_7"),
    # 多选 / 动态清单会让调用期校验不再逐值比对静态白名单
    (lambda m: _param(m, "lora_8").update(multiple=True), "multiple"),
    (lambda m: _param(m, "lora_8").update(options_source="comfy_styles",
                                          options_depends_on="prompt"), "options_source"),
    (lambda m: _param(m, "lora_1_strength").update(type="string"), "lora_1_strength"),
    (lambda m: _param(m, "lora_2_strength").pop("max"), "lora_2_strength"),
])
async def test_invalid_discovery_or_lora_mapping_rejected(client, mutate, needle):
    wf, _ = _artifact("nous-qwen21-text-to-image")
    r = await client.post("/api/v1/comfy-templates",
                          json={"name": "nous-qwen21-text-to-image", "workflow": wf})
    tid = r.json()["id"]
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=_t2i_with(mutate))
    assert r.status_code == 400, r.text
    assert needle in r.text


@pytest.mark.asyncio
async def test_service_patch_cannot_break_declared_lora_slots(client):
    """另一条 exposed_inputs 写路径(PATCH /api/v1/services/{id})同样受 discovery 约束。"""
    await _deploy(client, "nous-qwen21-text-to-image")
    svc = next(s for s in (await client.get("/api/v1/services")).json()
               if s["name"] == "nous-qwen21-text-to-image")
    detail = (await client.get(f"/api/v1/services/{svc['id']}")).json()
    inputs = detail["exposed_inputs"]

    r = await client.patch(f"/api/v1/services/{svc['id']}", json={"exposed_inputs": inputs})
    assert r.status_code == 200, r.text  # 原样写回:自洽,放行

    broken = [i for i in inputs if i["key"] != "lora_8"]
    r = await client.patch(f"/api/v1/services/{svc['id']}", json={"exposed_inputs": broken})
    assert r.status_code == 400, r.text
    assert "lora_8" in r.text

    free = [({**i, "constraints": {}} if i["key"] == "lora_3" else i) for i in inputs]
    r = await client.patch(f"/api/v1/services/{svc['id']}", json={"exposed_inputs": free})
    assert r.status_code == 400, r.text
    assert "lora_3" in r.text


@pytest.mark.asyncio
async def test_edit_or_upscale_without_required_media_rejected(client):
    wf, mapping = _artifact("nous-seedvr2-image-upscale")
    r = await client.post("/api/v1/comfy-templates",
                          json={"name": "nous-seedvr2-image-upscale", "workflow": wf})
    tid = r.json()["id"]
    params = [p for p in mapping["exposed_params"] if p["key"] != "image"]
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                         json={"discovery": mapping["discovery"], "exposed_params": params})
    assert r.status_code == 400, r.text
    assert "image_upscale" in r.text


# ---------- 公开目录 GET /v1/services ----------


async def _deploy_all(client) -> None:
    for service in ATOMIC:
        await _deploy(client, service)
    # 没有 discovery 的服务(比如视频放大)不进目录
    vid_wf = {"1": {"class_type": "VHS_VideoCombine", "inputs": {}}}
    r = await client.post("/api/v1/comfy-templates",
                          json={"name": "nous-vosr2-video-upscale", "workflow": vid_wf})
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_catalog_lists_only_discoverable_services_for_admin(client):
    await _deploy_all(client)
    r = await client.get("/v1/services")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "list"
    got = {s["service"]: s for s in body["data"]}
    assert set(got) == set(ATOMIC)
    for service, want in EXPECTED.items():
        item = got[service]
        assert {k: item[k] for k in want} == want
        assert item["schema_url"] == f"/v1/services/{service}/schema"
        assert "comfy_node_id" not in json.dumps(item)


@pytest.mark.asyncio
async def test_catalog_filters_by_operation(client):
    await _deploy_all(client)
    body = (await client.get("/v1/services", params={"operation": "image_upscale"})).json()
    assert sorted(s["display_name"] for s in body["data"]) == ["SeedVR2", "VOSR2"]
    r = await client.get("/v1/services", params={"operation": "nope"})
    assert r.status_code in (400, 422), r.text


@pytest.mark.asyncio
async def test_catalog_scoped_to_key_grants(client):
    await _deploy_all(client)
    key = await _mint_key("nous-qwen21-image-edit", "nous-vosr2-image-upscale")
    body = (await client.get("/v1/services", headers={"Authorization": f"Bearer {key}"})).json()
    assert sorted(s["service"] for s in body["data"]) == [
        "nous-qwen21-image-edit", "nous-vosr2-image-upscale"]


@pytest.mark.asyncio
async def test_catalog_hides_inactive_services(client):
    await _deploy_all(client)
    async with get_session_factory()() as s:
        svc = (await s.execute(select(ServiceInstance).where(
            ServiceInstance.name == "nous-vosr2-image-upscale"))).scalar_one()
        svc.status = "inactive"
        await s.commit()
    body = (await client.get("/v1/services")).json()
    assert "nous-vosr2-image-upscale" not in {s["service"] for s in body["data"]}
