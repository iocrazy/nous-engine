"""Skill preview/generate 驱动第一阶段四个原子图像服务(不改 skill-runs,只验证组合)。

- preview:Chat 模型执行内联 SKILL.md → 最终文本(上游 vLLM 打桩);
- generate:把文本写进 Qwen 文生图 / 图像编辑的 `prompt`,sidecar 用替身;
- 放大服务没有文本字段 → 不是 Skill 生成目标(prompt_fields 为空、generate 拒);
- Skill 内容只在 preview 的 chat 请求里出现,绝不进 ComfyUI 图、模板存储或任务输入。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

import src.services.nodes.comfy_bridge as nb
from src.models.api_gateway import ApiKeyGrant
from src.models.comfy_template import ComfyTemplate
from src.models.execution_task import ExecutionTask
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance

REPL = Path(__file__).resolve().parents[3] / "docs" / "replications"
ARTIFACTS = {
    "nous-qwen21-text-to-image": ("qwen21-text-to-image", "qwen21-text-to-image", "508"),
    "nous-qwen21-image-edit": ("qwen21-image-edit", "qwen21-image-edit", "487"),
    "nous-seedvr2-image-upscale": ("upscale", "seedvr2-image-upscale", "9"),
    "nous-vosr2-image-upscale": ("upscale", "vosr2-image-upscale", "4"),
}
# 独一无二的标记:Skill 正文里有它,Chat 模型的回复里没有 —— 用来追踪 Skill 内容流向。
SKILL_MARKER = "SKILL-BODY-7f3a9c"
SKILL = (f"---\nname: poster-prompt\n---\n\n{SKILL_MARKER}:把用户的想法扩写成一段画面描述,"
         "只输出描述本身。\n")
ENHANCED = "黄昏窗台上的橘猫,逆光,胶片颗粒"

PNG1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==")
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG1PX).decode()


@pytest.fixture
def fake_vllm(monkeypatch):
    """conftest api_client 的 model 服务 qwen3.5 指向 test-vllm.invalid,这里接住它。"""
    state = {"bodies": []}
    real_post = httpx.AsyncClient.post

    async def _post(self, url, *args, **kwargs):
        if "test-vllm.invalid" in str(url):
            state["bodies"].append(kwargs.get("json"))
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": ENHANCED},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
            }, request=httpx.Request("POST", str(url)))
        return await real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    async def _record(**_kw):
        return None
    import src.services.usage_service as usage_service
    monkeypatch.setattr(usage_service, "record_llm_usage", _record)
    return state


class _FakeComfy:
    def __init__(self):
        self.submitted: list[dict] = []
        self.out_node = "508"

    async def upload_image(self, filename, content, mime="image/png"):
        return f"up_{filename}"

    async def submit(self, graph):
        self.submitted.append(graph)
        return "p1"

    async def wait(self, prompt_id, *, timeout_s, interval_s=2.0, **_kw):
        return {"outputs": {self.out_node: {"images": [
                    {"filename": "out_00001_.png", "subfolder": "", "type": "output"}]}},
                "status": {"status_str": "success", "completed": True, "messages": []}}

    async def download(self, item):
        return b"PNGDATA"

    async def interrupt(self) -> None:
        return None


@pytest.fixture
def fake_comfy(monkeypatch):
    fc = _FakeComfy()
    monkeypatch.setattr(nb, "get_client", lambda: fc)

    async def _no_infos(class_types):  # 剪枝按「取不到声明」保守处理,不发任何 HTTP
        return {ct: None for ct in class_types}
    monkeypatch.setattr(nb, "get_node_infos", _no_infos)
    return fc


def _artifact(service: str) -> tuple[dict, dict]:
    d, prefix, _ = ARTIFACTS[service]
    return (json.loads((REPL / d / f"{prefix}.api.json").read_text()),
            json.loads((REPL / d / f"{prefix}.mapping.json").read_text()))


async def _deploy_and_grant(api_client, service: str) -> None:
    """建模板 + 写 mapping,并给 api_client 默认那把 key 加上该服务的 grant。"""
    wf, mapping = _artifact(service)
    r = await api_client.post("/api/v1/comfy-templates", json={"name": service, "workflow": wf})
    assert r.status_code == 201, r.text
    r = await api_client.put(f"/api/v1/comfy-templates/{r.json()['id']}/mapping", json=mapping)
    assert r.status_code == 200, r.text
    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        key = (await s.execute(select(InstanceApiKey))).scalars().first()
        svc = (await s.execute(
            select(ServiceInstance).where(ServiceInstance.name == service))).scalar_one()
        s.add(ApiKeyGrant(api_key_id=key.id, service_id=svc.id, status="active"))
        await s.commit()


async def _preview(api_client, bearer_headers) -> str:
    r = await api_client.post("/v1/skill-runs/preview", headers=bearer_headers, json={
        "model": "qwen3.5", "skill": {"content": SKILL}, "input": "窗台上的猫"})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == ENHANCED
    return r.json()["text"]


async def _generate(api_client, bearer_headers, service: str, text: str, inputs: dict):
    return await api_client.post("/v1/skill-runs/generate", headers=bearer_headers, json={
        "service": service, "prompt_field": "prompt", "text": text, "input": inputs})


@pytest.mark.asyncio
async def test_preview_generates_text_from_skill(api_client, bearer_headers, fake_vllm):
    await _preview(api_client, bearer_headers)
    sent = json.dumps(fake_vllm["bodies"][0], ensure_ascii=False)
    assert SKILL_MARKER in sent  # Skill 正文只去了 Chat 模型


@pytest.mark.asyncio
async def test_generate_injects_text_into_t2i_prompt(
        api_client, bearer_headers, fake_vllm, fake_comfy):
    service = "nous-qwen21-text-to-image"
    await _deploy_and_grant(api_client, service)
    text = await _preview(api_client, bearer_headers)
    r = await _generate(api_client, bearer_headers, service, text,
                        {"aspect_ratio": "1:1 (Square)",
                         "lora_1": "Qwen/Qwen-Image-2.1-viggle-turbo-4step-lora-r64.safetensors"})
    assert r.status_code == 200, r.text
    pred = r.json()
    assert pred["status"] == "succeeded", pred
    assert pred["output"]["outputs"]["out"]["image_url"]
    g = fake_comfy.submitted[0]
    assert g["517"]["inputs"]["value"] == ENHANCED
    assert g["512"]["inputs"]["aspect_ratio"] == "1:1 (Square)"
    assert g["700"]["inputs"]["lora_01"].startswith("Qwen/")


@pytest.mark.asyncio
async def test_generate_injects_text_into_image_edit_prompt(
        api_client, bearer_headers, fake_vllm, fake_comfy):
    service = "nous-qwen21-image-edit"
    fake_comfy.out_node = ARTIFACTS[service][2]
    await _deploy_and_grant(api_client, service)
    text = await _preview(api_client, bearer_headers)
    r = await _generate(api_client, bearer_headers, service, text,
                        {"image": PNG_URI, "image2": PNG_URI})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded", r.json()
    g = fake_comfy.submitted[0]
    assert g["571"]["inputs"]["value"] == ENHANCED
    assert g["475"]["inputs"]["image"].startswith("up_")


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["nous-seedvr2-image-upscale", "nous-vosr2-image-upscale"])
async def test_upscale_is_not_a_skill_target(api_client, bearer_headers, fake_comfy, service):
    await _deploy_and_grant(api_client, service)
    schema = (await api_client.get(f"/v1/services/{service}/schema")).json()
    assert schema["discovery"]["prompt_fields"] == []
    r = await _generate(api_client, bearer_headers, service, ENHANCED, {"image": PNG_URI})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_prompt_field"
    assert r.json()["error"]["text_fields"] == []
    assert fake_comfy.submitted == []
    # 目录里同样看得出来:只有带 prompt_fields 的服务才可作 Skill 目标
    cat = (await api_client.get("/v1/services")).json()["data"]
    assert {s["service"] for s in cat if s["prompt_fields"]} == set()


@pytest.mark.asyncio
async def test_skill_content_never_reaches_comfy_or_storage(
        api_client, bearer_headers, fake_vllm, fake_comfy):
    for service in ("nous-qwen21-text-to-image", "nous-qwen21-image-edit"):
        await _deploy_and_grant(api_client, service)
    text = await _preview(api_client, bearer_headers)
    fake_comfy.out_node = "508"
    r1 = await _generate(api_client, bearer_headers, "nous-qwen21-text-to-image", text, {})
    fake_comfy.out_node = "487"
    r2 = await _generate(api_client, bearer_headers, "nous-qwen21-image-edit", text,
                         {"image": PNG_URI})
    assert r1.json()["status"] == r2.json()["status"] == "succeeded"

    for graph in fake_comfy.submitted:
        blob = json.dumps(graph, ensure_ascii=False)
        assert SKILL_MARKER not in blob and "poster-prompt" not in blob
        classes = {n["class_type"] for n in graph.values()}
        assert not any("Prompt_Enhancer" in c or "Skill" in c for c in classes)
    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        stored = [t.workflow_json for t in (await s.execute(select(ComfyTemplate))).scalars()]
        svcs = (await s.execute(select(ServiceInstance))).scalars().all()
        for svc in svcs:
            await s.refresh(svc, attribute_names=["exposed_inputs", "workflow_snapshot"])
            stored += [svc.exposed_inputs, svc.workflow_snapshot, svc.discovery]
        stored += [t.input_json for t in (await s.execute(select(ExecutionTask))).scalars()]
    blob = json.dumps(stored, ensure_ascii=False)
    assert SKILL_MARKER not in blob and "poster-prompt" not in blob
