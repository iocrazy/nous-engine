"""服务详情页 Playground 调 LLM / embedding 服务 —— 走 admin 会话,不带 Authorization。

2026-09-22 用户报:qwen3-8-27b-huihui 的 Playground 点运行 → `Field required`,5ms 失败。
根因:/v1/chat/completions 与 /v1/embeddings 的鉴权依赖是 `verify_bearer_token_any`,
它把 `Authorization` 声明成**必填** Header(...)。浏览器里的 Playground 只带 admin 登录
cookie,没有 Authorization → FastAPI 参数校验直接 400「Field required」,业务代码一行
都没跑到。/v1/audio/transcriptions 早先踩过同一个坑、换成了「Bearer 优先,否则 admin
会话」的 `_auth_bearer_or_admin`;chat 与 embeddings 一直没跟上。

套件里 ADMIN_PASSWORD="" 关了登录闸 → 无 Authorization 的请求等同 admin 会话。
这里的模型都没加载,所以「修好」的判据是**越过鉴权、解析到服务、得到 503
model_not_ready**,而不是 400 Field required。绝不起真推理服务。
"""
import pytest

from src.models.service_instance import ServiceInstance


@pytest.fixture
def asked_engines(monkeypatch):
    """把 base_url 查找换成「记下被问的引擎 + 报未加载」。

    测试 app 的 model_manager 是 mock,不会自然抛 VLLMNotLoaded;这里让判据更硬:
    不只要求越过鉴权,还要求**解析到的正是那个服务的引擎**。
    """
    from src.api.routes import openai_compat
    from src.services.inference.vllm_endpoint import VLLMNotLoaded

    asked: list[str] = []

    def _fake(_mgr, engine_name):
        asked.append(engine_name)
        raise VLLMNotLoaded(engine_name)

    monkeypatch.setattr(openai_compat, "get_vllm_base_url", _fake)
    return asked


async def _svc(db_session, name: str, category: str) -> ServiceInstance:
    svc = ServiceInstance(
        name=name, type="inference", status="active",
        source_type="model", source_name=f"{name}-engine",
        category=category, meter_dim="tokens",
    )
    db_session.add(svc)
    await db_session.commit()
    return svc


@pytest.mark.asyncio
async def test_chat_completions_admin_session_reaches_service(db_session, db_client, asked_engines):
    await _svc(db_session, "pg-llm", "llm")
    r = await db_client.post("/v1/chat/completions", json={
        "model": "pg-llm", "messages": [{"role": "user", "content": "hi"}]})
    assert "Field required" not in r.text, r.text
    assert r.status_code == 503, r.text          # 越过鉴权 + 解析到服务;模型没加载
    assert "model_not_ready" in r.text
    assert asked_engines == ["pg-llm-engine"]


@pytest.mark.asyncio
async def test_chat_completions_admin_session_unknown_service_404(db_client):
    r = await db_client.post("/v1/chat/completions", json={
        "model": "no-such-svc", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_embeddings_admin_session_reaches_service(db_session, db_client, asked_engines):
    await _svc(db_session, "pg-emb", "embedding")
    r = await db_client.post("/v1/embeddings", json={"model": "pg-emb", "input": "hi"})
    assert "Field required" not in r.text, r.text
    assert r.status_code == 503, r.text
    assert "model_not_ready" in r.text
    assert asked_engines == ["pg-emb-engine"]


@pytest.mark.asyncio
async def test_chat_completions_admin_session_rejects_context_cache(db_session, db_client):
    """上下文缓存是按 key 归属的(owner_key_id);admin 会话没有 key,明确拒掉而不是崩。"""
    await _svc(db_session, "pg-llm2", "llm")
    r = await db_client.post("/v1/chat/completions", json={
        "model": "pg-llm2", "context_id": "ctx-1",
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400, r.text
    assert "context" in r.text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("/v1/chat/completions", {"model": "pg-llm3", "messages": [{"role": "user", "content": "hi"}]}),
    ("/v1/embeddings", {"model": "pg-llm3", "input": "hi"}),
])
async def test_no_key_no_session_is_401_when_login_gate_on(db_session, db_client, monkeypatch, path, body):
    """安全底线:admin 旁路**只**给已登录的 admin 会话。登录闸开着、既没 key 也没会话
    cookie → 401。否则这个旁路就成了数据面的免鉴权后门。"""
    from src.api import admin_session

    await _svc(db_session, "pg-llm3", "llm")
    monkeypatch.setattr(admin_session, "is_login_required", lambda: True)
    r = await db_client.post(path, json=body)
    assert r.status_code == 401, r.text
