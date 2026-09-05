"""Lane C-T2 + C-T3 · Ollama route integration tests.

Exercises the full stack: auth → resolve → translate → mock vLLM →
translate back. Uses the same `api_client` + `mock_vllm` fixtures as the
OpenAI surface so the test shapes stay comparable.
"""

from __future__ import annotations

import json
import secrets as _secrets

import bcrypt
import pytest
from unittest.mock import MagicMock

from src.models.api_gateway import ApiKeyGrant
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance


async def _make_mn_key(session_factory, *, instance_name: str):
    raw_key = f"sk-mn-{_secrets.token_hex(8)}"
    async with session_factory() as s:
        inst = ServiceInstance(
            source_type="model",
            source_name=instance_name,
            name=instance_name,
            type="llm",
            status="active",
        )
        s.add(inst)
        await s.commit()
        await s.refresh(inst)
        key = InstanceApiKey(
            instance_id=None, label="mn",
            key_hash=bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt()).decode(),
            key_prefix=raw_key[:10], is_active=True,
        )
        s.add(key)
        await s.commit()
        await s.refresh(key)
        s.add(ApiKeyGrant(
            api_key_id=key.id, service_id=inst.id, status="active",
        ))
        await s.commit()
        return raw_key, inst.id


# ---------- /api/chat ----------


@pytest.mark.asyncio
async def test_api_chat_non_stream(api_client, mock_vllm, bearer_headers):
    resp = await api_client.post(
        "/api/chat",
        json={
            "model": "qwen3.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        },
        headers=bearer_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == "qwen3.5"
    assert data["message"]["role"] == "assistant"
    assert data["done"] is True


@pytest.mark.asyncio
async def test_api_chat_stream_ndjson(
    api_client, mock_vllm, bearer_headers, monkeypatch,
):
    """Stub vLLM's SSE stream with two content chunks + a terminal chunk,
    confirm the route translates to NDJSON with done=False,False,True."""
    import httpx

    class _FakeStreamCtx:
        def __init__(self):
            self.status_code = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"role":"assistant"},"index":0}]}'
            yield 'data: {"choices":[{"delta":{"content":"He"},"index":0}]}'
            yield 'data: {"choices":[{"delta":{"content":"llo"},"index":0}]}'
            yield 'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}'
            yield 'data: [DONE]'
        async def aread(self):
            return b""

    real_stream = httpx.AsyncClient.stream

    def _stream_patch(self, method, url, **kwargs):
        from httpx import URL as _URL
        base = getattr(self, "base_url", None)
        raw = _URL(url) if not isinstance(url, _URL) else url
        full = base.join(raw) if base else raw
        host = full.host or ""
        if host == "test-vllm.invalid":
            return _FakeStreamCtx()
        return real_stream(self, method, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "stream", _stream_patch)

    async with api_client.stream(
        "POST",
        "/api/chat",
        json={
            "model": "qwen3.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        headers=bearer_headers,
    ) as resp:
        assert resp.status_code == 200
        assert "application/x-ndjson" in resp.headers.get("content-type", "")
        lines = []
        async for line in resp.aiter_lines():
            s = line.strip()
            if s:
                lines.append(s)

    parsed = [json.loads(ln) for ln in lines]
    assert parsed[-1]["done"] is True
    assert parsed[-1]["done_reason"] == "stop"
    assert any(obj.get("done") is False for obj in parsed[:-1])
    contents = [o["message"]["content"] for o in parsed if not o["done"]]
    assert "".join(contents) == "Hello"


@pytest.mark.asyncio
async def test_api_chat_unknown_model_returns_404_for_mn_key(
    api_client, mock_vllm,
):
    sf = api_client.app.state.async_session_factory
    raw_key, _ = await _make_mn_key(sf, instance_name="qwen-preview")
    resp = await api_client.post(
        "/api/chat",
        json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_chat_inactive_service_403_for_mn_key(api_client, mock_vllm):
    """round9:M:N key 命中 inactive service → 403(与 openai/anthropic 一致,早先 ollama 漏检查)。"""
    sf = api_client.app.state.async_session_factory
    raw_key, inst_id = await _make_mn_key(sf, instance_name="paused-svc")
    # 把 service 置 inactive(grant 仍 active)
    from sqlalchemy import update
    async with sf() as s:
        await s.execute(update(ServiceInstance).where(ServiceInstance.id == inst_id).values(status="inactive"))
        await s.commit()
    resp = await api_client.post(
        "/api/chat",
        json={"model": "paused-svc", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 403


# ---------- /api/generate ----------


@pytest.mark.asyncio
async def test_api_generate_non_stream(api_client, mock_vllm, bearer_headers):
    resp = await api_client.post(
        "/api/generate",
        json={"model": "qwen3.5", "prompt": "hello", "stream": False},
        headers=bearer_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == "qwen3.5"
    assert "response" in data
    assert data["done"] is True


# ---------- /api/tags ----------


@pytest.mark.asyncio
async def test_api_tags_key_sees_granted_service(
    api_client, mock_vllm, bearer_headers,
):
    resp = await api_client.get("/api/tags", headers=bearer_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "models" in data
    names = [m["name"] for m in data["models"]]
    # Default fixture's M:N key is granted the service named "qwen3.5".
    assert any("qwen3.5" in n for n in names)


@pytest.mark.asyncio
async def test_api_tags_mn_key_sees_only_granted(api_client, mock_vllm):
    sf = api_client.app.state.async_session_factory
    raw_key, _ = await _make_mn_key(sf, instance_name="only-this-one")
    resp = await api_client.get(
        "/api/tags", headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 200
    names = [m["name"] for m in resp.json()["models"]]
    assert names == ["only-this-one"]


@pytest.mark.asyncio
async def test_api_tags_hides_cold_model_service(
    api_client, mock_vllm, bearer_headers,
):
    """spec 2026-09-05 §6:发现到的 == 现在就能调的,口径与 /v1/models 一致。

    修前 Ollama 面把冷模型也列出来,Open WebUI 之类的客户端选中即 503。
    """
    api_client.app.state.model_manager.is_loaded = MagicMock(return_value=False)
    resp = await api_client.get("/api/tags", headers=bearer_headers)
    assert resp.status_code == 200
    assert [m["name"] for m in resp.json()["models"]] == []


@pytest.mark.asyncio
async def test_api_tags_shows_loaded_model_service(
    api_client, mock_vllm, bearer_headers,
):
    """同一把 key、同一个服务,模型已加载就必须出现 —— 反证上面那条不是"永远空"。"""
    api_client.app.state.model_manager.is_loaded = MagicMock(
        side_effect=lambda n: n == "qwen3.5",
    )
    resp = await api_client.get("/api/tags", headers=bearer_headers)
    assert resp.status_code == 200
    assert [m["name"] for m in resp.json()["models"]] == ["qwen3.5"]


@pytest.mark.asyncio
async def test_api_chat_on_cold_model_is_503_model_not_ready(
    api_client, mock_vllm, bearer_headers,
):
    """spec §5:Ollama 面的未就绪拒绝也走 model_not_ready 信封,且绝不加载。"""
    mgr = api_client.app.state.model_manager
    mgr.get_adapter = MagicMock(return_value=None)      # 冷:没有 adapter

    resp = await api_client.post(
        "/api/chat",
        json={
            "model": "qwen3.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        },
        headers=bearer_headers,
    )
    assert resp.status_code == 503, resp.text
    err = resp.json()["error"]
    assert err["type"] == "model_not_ready" and err["code"] == "model_not_ready"
    assert "/v1/models" in err["fix"]
    mgr.load_model.assert_not_called()


# ---------- /api/show ----------


@pytest.mark.asyncio
async def test_api_show_returns_details(api_client, mock_vllm, bearer_headers):
    resp = await api_client.post(
        "/api/show",
        json={"name": "qwen3.5"},
        headers=bearer_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "details" in data
    assert data["details"]["format"] == "gguf"


@pytest.mark.asyncio
async def test_api_show_missing_name_400(api_client, mock_vllm, bearer_headers):
    resp = await api_client.post(
        "/api/show", json={}, headers=bearer_headers,
    )
    assert resp.status_code == 400
