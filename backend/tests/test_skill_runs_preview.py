"""POST /v1/skill-runs/preview(spec 2026-09-26 skill-runs §3.1)。

用 conftest 的 api_client:预置 model 服务 "qwen3.5"(引擎已"加载",base_url=test-vllm.invalid)+
一把对它有 grant 的 M:N key。上游 vLLM 用本文件的 fake_vllm 打桩(可配回复)。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from src.models.service_instance import ServiceInstance

SKILL = "---\nname: expand\n---\n\n把用户的想法扩写成一段画面描述,只输出描述本身。\n"


def _reply(content, finish="stop"):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
    }


@pytest.fixture
def fake_vllm(monkeypatch):
    state = {"reply": _reply("  a ginger cat on a windowsill, golden hour  "),
             "status": 200, "bodies": [], "usage": []}
    real_post = httpx.AsyncClient.post

    async def _post(self, url, *args, **kwargs):
        if "test-vllm.invalid" in str(url):
            state["bodies"].append(kwargs.get("json"))
            return httpx.Response(state["status"], json=state["reply"],
                                  request=httpx.Request("POST", str(url)))
        return await real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    async def _record(**kw):
        state["usage"].append(kw)
    import src.services.usage_service as usage_service
    monkeypatch.setattr(usage_service, "record_llm_usage", _record)
    return state


def _req(**over):
    body = {"model": "qwen3.5", "skill": {"content": SKILL}, "input": "黄昏窗台上的猫"}
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_preview_returns_final_text(api_client, bearer_headers, fake_vllm):
    r = await api_client.post("/v1/skill-runs/preview", json=_req(), headers=bearer_headers)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["text"] == "a ginger cat on a windowsill, golden hour"
    assert out["model"] == "qwen3.5"
    assert out["skill"] == {"name": "expand"}
    assert out["finish_reason"] == "stop"
    assert out["usage"] == {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}

    sent = fake_vllm["bodies"][0]
    assert sent["messages"][0] == {
        "role": "system", "content": "把用户的想法扩写成一段画面描述,只输出描述本身。"}
    assert sent["messages"][1] == {"role": "user", "content": "黄昏窗台上的猫"}
    assert sent["model"] == ""
    assert "thinking" not in sent                              # 已翻译成 chat_template_kwargs
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}   # 默认 disabled(qwen 白名单)
    assert len(fake_vllm["usage"]) == 1                        # 按普通 chat 记用量


@pytest.mark.asyncio
async def test_preview_content_parts_with_image(api_client, bearer_headers, fake_vllm):
    parts = [{"type": "text", "text": "把背景换成雪夜"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]
    r = await api_client.post("/v1/skill-runs/preview", json=_req(input=parts), headers=bearer_headers)
    assert r.status_code == 200, r.text
    assert fake_vllm["bodies"][0]["messages"][1]["content"] == parts


@pytest.mark.asyncio
async def test_preview_rejects_private_image_url(api_client, bearer_headers, fake_vllm):
    parts = [{"type": "image_url", "image_url": {"url": "http://169.254.169.254/x.png"}}]
    r = await api_client.post("/v1/skill-runs/preview", json=_req(input=parts), headers=bearer_headers)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "unsafe_image_url"
    assert fake_vllm["bodies"] == []


@pytest.mark.asyncio
async def test_preview_rejects_unsupported_content_part(api_client, bearer_headers, fake_vllm):
    parts = [{"type": "text", "text": "看这段视频"},
             {"type": "video_url", "video_url": {"url": "http://example.com/v.mp4"}}]
    r = await api_client.post("/v1/skill-runs/preview", json=_req(input=parts), headers=bearer_headers)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_input_part"
    assert fake_vllm["bodies"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("big_input", [
    "猫" * 100_001,                                      # 字符串超 100k 字符
    [{"type": "text", "text": "x"}] * 33,                # 超 32 个 part
], ids=["str_over_100k", "parts_over_32"])
async def test_preview_input_size_caps(api_client, bearer_headers, fake_vllm, big_input):
    r = await api_client.post("/v1/skill-runs/preview", json=_req(input=big_input),
                              headers=bearer_headers)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "validation_error"
    assert fake_vllm["bodies"] == []


@pytest.mark.asyncio
async def test_preview_admin_session_without_bearer(api_client, fake_vllm):
    r = await api_client.post("/v1/skill-runs/preview", json=_req())
    assert r.status_code == 200, r.text
    assert fake_vllm["usage"][0]["api_key_id"] is None


@pytest.mark.asyncio
async def test_preview_cold_model_is_503_and_never_loads(api_client, bearer_headers, fake_vllm):
    mgr = api_client.app.state.model_manager
    mgr.get_adapter = MagicMock(return_value=None)
    r = await api_client.post("/v1/skill-runs/preview", json=_req(), headers=bearer_headers)
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "model_not_ready"
    mgr.load_model.assert_not_called()
    assert fake_vllm["bodies"] == []


@pytest.mark.asyncio
async def test_preview_unknown_or_ungranted_model_is_404(api_client, bearer_headers, fake_vllm):
    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        s.add(ServiceInstance(source_type="model", source_name="other", name="other-llm",
                              type="llm", status="active"))
        await s.commit()
    for name in ("nope", "other-llm"):          # 不存在 / 存在但这把 key 没 grant
        r = await api_client.post("/v1/skill-runs/preview", json=_req(model=name),
                                  headers=bearer_headers)
        assert r.status_code == 404, (name, r.text)


@pytest.mark.asyncio
async def test_preview_non_model_service_is_400(api_client, fake_vllm):
    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        s.add(ServiceInstance(source_type="workflow", source_id=1, name="some-flow",
                              type="inference", status="active"))
        await s.commit()
    r = await api_client.post("/v1/skill-runs/preview", json=_req(model="some-flow"))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "not_a_chat_model"


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "code"), [
    (_reply("half a prom", finish="length"), "skill_output_truncated"),
    (_reply("   "), "skill_empty_output"),
])
async def test_preview_unusable_output_is_502(api_client, bearer_headers, fake_vllm, reply, code):
    fake_vllm["reply"] = reply
    r = await api_client.post("/v1/skill-runs/preview", json=_req(), headers=bearer_headers)
    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == code
    assert len(fake_vllm["usage"]) == 1              # 上游已生成 token:502 也照记用量


@pytest.mark.asyncio
async def test_preview_upstream_error_passthrough(api_client, bearer_headers, fake_vllm):
    fake_vllm["status"] = 400
    fake_vllm["reply"] = {"error": {"message": "bad image"}}
    r = await api_client.post("/v1/skill-runs/preview", json=_req(), headers=bearer_headers)
    assert r.status_code == 400
    assert r.json() == {"error": {"message": "bad image"}}
    assert fake_vllm["usage"] == []                      # 失败不记账


@pytest.mark.asyncio
@pytest.mark.parametrize(("over", "status", "code"), [
    ({"skill": {"content": "---\nname: x\n---\n  "}}, 422, "skill_empty"),
    ({"input": ""}, 422, "empty_input"),
    ({"options": {"thinking": "maybe"}}, 400, "validation_error"),
    ({"options": {"max_tokens": 0}}, 400, "validation_error"),
])
async def test_preview_request_validation(api_client, bearer_headers, fake_vllm, over, status, code):
    r = await api_client.post("/v1/skill-runs/preview", json=_req(**over), headers=bearer_headers)
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code
    assert fake_vllm["bodies"] == []


@pytest.mark.asyncio
async def test_preview_clamps_max_tokens(api_client, bearer_headers, fake_vllm):
    r = await api_client.post("/v1/skill-runs/preview",
                              json=_req(options={"max_tokens": 100000}), headers=bearer_headers)
    assert r.status_code == 200, r.text
    assert fake_vllm["bodies"][0]["max_tokens"] == 3584   # api_client 的 max_model_len=4096


@pytest.mark.asyncio
async def test_preview_quota_exhausted_is_402_and_never_calls_upstream(
        api_client, bearer_headers, fake_vllm):
    from sqlalchemy import select

    from src.models.api_gateway import ApiKeyGrant, ResourcePack
    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        grant = (await s.execute(select(ApiKeyGrant))).scalars().first()
        s.add(ResourcePack(grant_id=grant.id, name="spent", total_units=10, used_units=10))
        await s.commit()
    r = await api_client.post("/v1/skill-runs/preview", json=_req(), headers=bearer_headers)
    assert r.status_code == 402, r.text
    assert fake_vllm["bodies"] == []
