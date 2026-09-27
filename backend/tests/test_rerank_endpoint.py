"""POST /v1/rerank(2026-09-27,Qwen3-VL-Reranker 接入)。

Cohere/Jina 形:{model, query, documents[, top_n, return_documents]};query / 每个 document 可为
字符串或 {"content": [text/image_url parts]}。经 chat 共用层(鉴权/grant/配额/未加载 503/用量),
转发到该服务 vLLM pooling 实例的 /v1/rerank。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import select

from src.models.api_gateway import ApiKeyGrant, ResourcePack
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance

RR = "nous-rr-test"


def _reply():
    return {
        "id": "rerank-1", "model": "", "usage": {"prompt_tokens": 42, "total_tokens": 42},
        "results": [
            {"index": 0, "relevance_score": 0.9, "document": {"text": "a"}},
            {"index": 1, "relevance_score": 0.1, "document": {"text": "b"}},
        ],
    }


@pytest.fixture
def fake_vllm(monkeypatch):
    state = {"reply": _reply(), "status": 200, "calls": [], "usage": []}
    real_post = httpx.AsyncClient.post

    async def _post(self, url, *args, **kwargs):
        if "test-vllm.invalid" in str(url):
            state["calls"].append((str(url), kwargs.get("json")))
            return httpx.Response(state["status"], json=state["reply"],
                                  request=httpx.Request("POST", str(url)))
        return await real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    async def _record(**kw):
        state["usage"].append(kw)
    import src.services.usage_service as usage_service
    monkeypatch.setattr(usage_service, "record_llm_usage", _record)
    return state


@pytest.fixture
async def rr_service(api_client):
    """rerank 类 model 服务,并授权给 api_client 那把 key。"""
    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        svc = ServiceInstance(source_type="model", source_name="qwen_vl_rr", name=RR,
                              type="inference", status="active", category="rerank")
        s.add(svc)
        await s.commit()
        await s.refresh(svc)
        key = (await s.execute(select(InstanceApiKey))).scalars().first()
        s.add(ApiKeyGrant(api_key_id=key.id, service_id=svc.id, status="active"))
        await s.commit()
    return svc


def _req(**over):
    body = {"model": RR, "query": "橘猫", "documents": ["猫在窗边", "股市下跌"], "top_n": 2}
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_rerank_forwards_to_vllm_rerank(api_client, bearer_headers, fake_vllm, rr_service):
    r = await api_client.post("/v1/rerank", json=_req(), headers=bearer_headers)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["model"] == RR                                   # 回写成服务名
    assert [x["index"] for x in out["results"]] == [0, 1]
    url, sent = fake_vllm["calls"][0]
    assert url.endswith("/v1/rerank")
    assert sent["model"] == ""                                  # vLLM 用自己的 served 名
    assert sent["query"] == "橘猫" and sent["documents"] == ["猫在窗边", "股市下跌"]
    assert sent["top_n"] == 2
    assert fake_vllm["usage"][0]["prompt_tokens"] == 42
    assert fake_vllm["usage"][0]["completion_tokens"] == 0


@pytest.mark.asyncio
async def test_rerank_multimodal_documents_pass_through(api_client, bearer_headers, fake_vllm, rr_service):
    img = {"content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]}
    r = await api_client.post("/v1/rerank", headers=bearer_headers,
                              json=_req(query={"content": [{"type": "text", "text": "猫"}]},
                                        documents=[img, "文本文档"]))
    assert r.status_code == 200, r.text
    assert fake_vllm["calls"][0][1]["documents"][0] == img


@pytest.mark.asyncio
@pytest.mark.parametrize("docs", [
    [{"content": [{"type": "image_url", "image_url": {"url": "http://169.254.169.254/x.png"}}]}],
    [{"content": [{"type": "image_url", "image_url": {"url": "https://127.0.0.1/x.png"}}]}],
])
async def test_rerank_blocks_private_image_urls(api_client, bearer_headers, fake_vllm, rr_service, docs):
    r = await api_client.post("/v1/rerank", json=_req(documents=docs), headers=bearer_headers)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "unsafe_image_url"
    assert fake_vllm["calls"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("docs", [
    [{"content": [{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA"}}]}],
    [{"content": [{"image_url": "http://10.0.0.1/x.png"}]}],                    # 无 type
])
async def test_rerank_rejects_non_whitelisted_parts(api_client, bearer_headers, fake_vllm, rr_service, docs):
    r = await api_client.post("/v1/rerank", json=_req(documents=docs), headers=bearer_headers)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_input_part"
    assert fake_vllm["calls"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [
    {"documents": []},
    {"documents": ["x"] * 257},
    {"top_n": 0},
    {"query": ""},
])
async def test_rerank_request_validation(api_client, bearer_headers, fake_vllm, rr_service, over):
    r = await api_client.post("/v1/rerank", json=_req(**over), headers=bearer_headers)
    assert r.status_code in (400, 422), r.text
    assert fake_vllm["calls"] == []


@pytest.mark.asyncio
async def test_rerank_on_non_rerank_service_is_400(api_client, bearer_headers, fake_vllm):
    r = await api_client.post("/v1/rerank", json=_req(model="qwen3.5"), headers=bearer_headers)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "not_a_rerank_model"
    assert fake_vllm["calls"] == []


@pytest.mark.asyncio
async def test_rerank_cold_model_is_503_and_never_loads(api_client, bearer_headers, fake_vllm, rr_service):
    mgr = api_client.app.state.model_manager
    mgr.get_adapter = MagicMock(return_value=None)
    r = await api_client.post("/v1/rerank", json=_req(), headers=bearer_headers)
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "model_not_ready"
    mgr.load_model.assert_not_called()


@pytest.mark.asyncio
async def test_rerank_quota_exhausted_is_402(api_client, bearer_headers, fake_vllm, rr_service):
    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        grant = (await s.execute(select(ApiKeyGrant).where(
            ApiKeyGrant.service_id == rr_service.id))).scalars().first()
        s.add(ResourcePack(grant_id=grant.id, name="spent", total_units=5, used_units=5))
        await s.commit()
    r = await api_client.post("/v1/rerank", json=_req(), headers=bearer_headers)
    assert r.status_code == 402, r.text
    assert fake_vllm["calls"] == []


@pytest.mark.asyncio
async def test_rerank_upstream_error_passthrough_not_billed(api_client, bearer_headers, fake_vllm, rr_service):
    fake_vllm["status"] = 400
    fake_vllm["reply"] = {"error": {"message": "bad doc"}}
    r = await api_client.post("/v1/rerank", json=_req(), headers=bearer_headers)
    assert r.status_code == 400
    assert r.json() == {"error": {"message": "bad doc"}}
    assert fake_vllm["usage"] == []


@pytest.mark.asyncio
async def test_rerank_admin_session_without_bearer(api_client, fake_vllm, rr_service):
    r = await api_client.post("/v1/rerank", json=_req())
    assert r.status_code == 200, r.text
    assert fake_vllm["usage"][0]["api_key_id"] is None
