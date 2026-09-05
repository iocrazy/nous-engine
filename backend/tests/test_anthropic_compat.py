"""Anthropic /v1/messages 适配端点测试 — 非流式 happy path + 错误。

vLLM 上游用 httpx mock 截掉，不真起 model_manager。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import bcrypt
import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.api.main import create_app
from src.models.api_gateway import ApiKeyGrant
from src.models.database import get_async_session
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance


@pytest.fixture
async def db_app_client(pg_engine):
    """db_client + 暴露 app（让测试能挂 model_manager mock）。"""
    engine = pg_engine   # 全局只用 PostgreSQL;表由 pg_engine fixture 建好
    sf = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with sf() as session:
            yield session

    app = create_app()
    # Mock model manager（与 conftest._mock_model_manager 等价但要可后续覆盖）
    mgr = MagicMock()
    mgr.loaded_model_ids = []
    app.state.model_manager = mgr
    app.dependency_overrides[get_async_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield app, c, sf



@pytest.fixture
async def loaded_service(db_app_client):
    _, _, sf = db_app_client
    async with sf() as s:
        svc = ServiceInstance(
            name="qwen-svc", type="inference", status="active",
            source_type="model", source_name="qwen-test",
            category="llm", meter_dim="tokens",
        )
        s.add(svc)
        await s.commit()
        await s.refresh(svc)
        return svc


@pytest.fixture
async def grant_key(db_app_client, loaded_service):
    _, _, sf = db_app_client
    secret = "sk-anthr-fixedtoken1234567890abcdef"
    async with sf() as s:
        key = InstanceApiKey(
            instance_id=None, label="anthropic-test",
            key_hash=bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode(),
            key_prefix=secret[:10],
            secret_plaintext=secret,
        )
        s.add(key)
        await s.flush()
        s.add(ApiKeyGrant(api_key_id=key.id, service_id=loaded_service.id))
        await s.commit()
        await s.refresh(key)
        return secret, key


def _wire_loaded_model(app, base_url: str = "http://upstream.test:9999"):
    adapter = MagicMock()
    adapter.is_loaded = True
    adapter.base_url = base_url
    app.state.model_manager.get_adapter = MagicMock(return_value=adapter)


@pytest.fixture
def stub_usage_recorder(monkeypatch):
    """record_llm_usage 自己开 engine 连 DATABASE_URL — CI 没 PG 跑会爆。
    把它打成 no-op，匹配 conftest 里 _llm_recorder 的做法。"""
    async def _noop(**kwargs):
        return None
    import src.services.usage_service as _usage
    monkeypatch.setattr(_usage, "record_llm_usage", _noop)


class _FakeUpstreamClient:
    """Stand-in for httpx.AsyncClient used inside anthropic_compat.

    Only intercepts the POST inside the route handler — the outer test
    client (also an AsyncClient over ASGI) is untouched because we patch
    the symbol via module attribute, not the class.
    """
    def __init__(self, response_json, *, status: int = 200):
        self._json = response_json
        self._status = status
        self.last_url: str | None = None
        self.last_body: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, json=None, **kw):
        self.last_url = url
        self.last_body = json
        return httpx.Response(self._status, json=self._json)


def _patch_upstream(monkeypatch, response_json, status=200):
    fake = _FakeUpstreamClient(response_json, status=status)
    from src.api.routes import anthropic_compat
    monkeypatch.setattr(
        anthropic_compat.httpx, "AsyncClient",
        lambda *a, **kw: fake,
    )
    return fake


@pytest.mark.asyncio
async def test_anthropic_messages_non_streaming(
    db_app_client, grant_key, monkeypatch, stub_usage_recorder,
):
    app, db_client, _ = db_app_client
    secret, _ = grant_key
    _wire_loaded_model(app)

    upstream = _patch_upstream(monkeypatch, {
        "choices": [{
            "message": {"role": "assistant", "content": "Hi there."},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    })

    r = await db_client.post(
        "/v1/messages",
        headers={"x-api-key": secret},
        json={
            "model": "qwen-svc",
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "Say hi"}],
            "max_tokens": 64,
        },
    )

    assert r.status_code == 200, r.text
    assert "/v1/chat/completions" in upstream.last_url
    assert upstream.last_body["messages"][0] == {
        "role": "system", "content": "You are helpful.",
    }
    assert upstream.last_body["messages"][1] == {
        "role": "user", "content": "Say hi",
    }

    data = r.json()
    assert data["type"] == "message"
    assert data["role"] == "assistant"
    assert data["content"] == [{"type": "text", "text": "Hi there."}]
    assert data["stop_reason"] == "end_turn"
    assert data["usage"] == {"input_tokens": 4, "output_tokens": 3}
    assert data["id"].startswith("msg_")


@pytest.mark.asyncio
async def test_anthropic_messages_accepts_bearer_too(
    db_app_client, grant_key, monkeypatch, stub_usage_recorder,
):
    app, db_client, _ = db_app_client
    secret, _ = grant_key
    _wire_loaded_model(app)
    _patch_upstream(monkeypatch, {
        "choices": [{
            "message": {"content": "ok"}, "finish_reason": "length",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })

    r = await db_client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {secret}"},
        json={
            "model": "qwen-svc",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 32,
        },
    )

    assert r.status_code == 200
    assert r.json()["stop_reason"] == "max_tokens"


@pytest.mark.asyncio
async def test_anthropic_messages_on_cold_model_is_503_model_not_ready(
    db_app_client, grant_key,
):
    """spec 2026-09-05 §5:Anthropic 面的未就绪拒绝也是 model_not_ready 信封,且绝不加载。

    这条 503 此前一个断言都没有(2026-09-05 复审 E8):信封退化成 type=api_error /
    code=null 也不会有人发现,而下游正是按 code 分流的。
    """
    app, db_client, _ = db_app_client
    secret, _ = grant_key
    app.state.model_manager.get_adapter = MagicMock(return_value=None)   # 冷:没有 adapter

    r = await db_client.post(
        "/v1/messages",
        headers={"x-api-key": secret},
        json={
            "model": "qwen-svc",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 32,
        },
    )
    assert r.status_code == 503, r.text
    err = r.json()["error"]
    assert err["type"] == "model_not_ready" and err["code"] == "model_not_ready"
    assert err["param"] == "model"
    assert "/v1/models" in err["fix"]
    app.state.model_manager.load_model.assert_not_called()


@pytest.mark.asyncio
async def test_anthropic_messages_rejects_no_auth(db_app_client):
    _, db_client, _ = db_app_client
    r = await db_client.post(
        "/v1/messages",
        json={
            "model": "qwen-svc",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_anthropic_messages_rejects_streaming(db_app_client, grant_key):
    _, db_client, _ = db_app_client
    secret, _ = grant_key
    r = await db_client.post(
        "/v1/messages",
        headers={"x-api-key": secret},
        json={
            "model": "qwen-svc",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    # 显式不支持流式 → 400 with explanatory message.
    assert r.status_code in (400, 422)
    assert "stream" in r.text.lower()


@pytest.mark.asyncio
async def test_content_blocks_are_flattened(
    db_app_client, grant_key, monkeypatch, stub_usage_recorder,
):
    """Anthropic 允许 messages[].content 为 [{type:"text",text:"..."}, ...]。
    适配器应把 text blocks 拼成单段 string 给上游。"""
    app, db_client, _ = db_app_client
    secret, _ = grant_key
    _wire_loaded_model(app)
    upstream = _patch_upstream(monkeypatch, {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })

    r = await db_client.post(
        "/v1/messages",
        headers={"x-api-key": secret},
        json={
            "model": "qwen-svc",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "text", "text": "b"},
                    {"type": "text", "text": "c"},
                ],
            }],
            "max_tokens": 16,
        },
    )
    assert r.status_code == 200
    # 三个 text blocks 拍扁成 "abc"
    assert upstream.last_body["messages"][0]["content"] == "abc"
