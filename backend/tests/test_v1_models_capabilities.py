"""GET /v1/models 与 /v1/models/{id} 带 context_window + capabilities(2026-09-24)。

用途:nous-app 的 admin 自动同步「nous 行的上下文窗口」,不用人填。
- context_window = **生效值**(yaml 叠加运行时覆盖 = vLLM 实际按它启动的值),不是模型原生上限。
- `?include_unready=1`:默认行为不变(只列 / 只认已加载的,09-05「发现到的 == 能调的」);
  带上它则返回**全部已授权**服务,每条附 `ready`,同步一次拿全,不因模型闲置被卸载而漏。
"""
from __future__ import annotations

import bcrypt
import pytest

from src.models.api_gateway import ApiKeyGrant
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance

_CFG = {
    "eng-hot": {
        "type": "llm", "paths": {"main": "llm/none"}, "source": "huihui-ai/Huihui-X",
        "params": {"max_model_len": 32768, "vllm_args": {
            "reasoning-parser": "qwen3", "enable-auto-tool-choice": True}},
    },
    "eng-cold": {
        "type": "llm", "paths": {"main": "llm/none2"}, "source": "orcarouter/Y",
        "params": {"max_model_len": 262144},
    },
}


@pytest.fixture
def fake_env(monkeypatch):
    """假引擎配置 + 就绪判定:eng-hot 已加载、eng-cold 未加载。不碰真模型、不起进程。"""
    from src.api.routes import _readiness
    import src.config as config

    monkeypatch.setattr(config, "load_model_configs", lambda *a, **k: _CFG)
    monkeypatch.setattr(
        _readiness, "service_is_ready",
        lambda _mgr, svc: svc.source_type != "model" or svc.source_name == "eng-hot")


async def _setup(db_session):
    svcs = [
        ServiceInstance(source_type="model", source_name="eng-hot", name="hot-llm",
                        type="inference", status="active", category="llm"),
        ServiceInstance(source_type="model", source_name="eng-cold", name="cold-llm",
                        type="inference", status="active", category="llm"),
        ServiceInstance(source_type="workflow", source_name="x", name="wf-img",
                        type="inference", status="active", category="image"),
    ]
    db_session.add_all(svcs)
    raw = "sk-cap12345abcdef"
    key = InstanceApiKey(instance_id=None, label="t",
                         key_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
                         key_prefix=raw[:10], is_active=True)
    db_session.add(key)
    await db_session.commit()
    for s in svcs:
        await db_session.refresh(s)
    await db_session.refresh(key)
    db_session.add_all([ApiKeyGrant(api_key_id=key.id, service_id=s.id, status="active") for s in svcs])
    await db_session.commit()
    return {"Authorization": f"Bearer {raw}"}


@pytest.mark.asyncio
async def test_list_default_unchanged_but_carries_capabilities(db_client, db_session, fake_env):
    h = await _setup(db_session)
    data = {m["id"]: m for m in (await db_client.get("/v1/models", headers=h)).json()["data"]}
    assert set(data) == {"hot-llm", "wf-img"}          # 默认只列已就绪的(行为不变)
    hot = data["hot-llm"]
    assert hot["context_window"] == 32768
    assert hot["ready"] is True
    c = hot["capabilities"]
    assert (c["tools"], c["thinking"], c["provider"], c["context"]) == (True, True, "huihui-ai", 32768)
    assert data["wf-img"]["context_window"] is None     # 非模型服务不编
    assert data["wf-img"]["capabilities"] is None


@pytest.mark.asyncio
async def test_list_include_unready_returns_all_granted_with_ready_flag(db_client, db_session, fake_env):
    h = await _setup(db_session)
    data = {m["id"]: m for m in
            (await db_client.get("/v1/models?include_unready=1", headers=h)).json()["data"]}
    assert set(data) == {"hot-llm", "cold-llm", "wf-img"}
    assert data["cold-llm"]["ready"] is False
    assert data["cold-llm"]["context_window"] == 262144  # 没加载也拿得到(来自配置)
    assert data["cold-llm"]["capabilities"]["provider"] == "orcarouter"


@pytest.mark.asyncio
async def test_get_one_unready_is_503_by_default_200_with_flag(db_client, db_session, fake_env):
    h = await _setup(db_session)
    assert (await db_client.get("/v1/models/cold-llm", headers=h)).status_code == 503
    r = await db_client.get("/v1/models/cold-llm?include_unready=1", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready"] is False and body["context_window"] == 262144


@pytest.mark.asyncio
async def test_get_one_ready_has_fields(db_client, db_session, fake_env):
    h = await _setup(db_session)
    body = (await db_client.get("/v1/models/hot-llm", headers=h)).json()
    assert body["context_window"] == 32768 and body["ready"] is True
    assert body["capabilities"]["source"] == "huihui-ai/Huihui-X"


@pytest.mark.asyncio
async def test_include_unready_never_leaks_ungranted(db_client, db_session, fake_env):
    """include_unready 只放宽「就绪」,**不放宽授权**:没授权的服务照样看不到。"""
    h = await _setup(db_session)
    db_session.add(ServiceInstance(source_type="model", source_name="eng-cold", name="secret",
                                   type="inference", status="active", category="llm"))
    await db_session.commit()
    ids = {m["id"] for m in (await db_client.get("/v1/models?include_unready=1", headers=h)).json()["data"]}
    assert "secret" not in ids
    assert (await db_client.get("/v1/models/secret?include_unready=1", headers=h)).status_code == 404
