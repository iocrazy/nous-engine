"""/v1/models 对桥服务(comfy_template)的 ready 要反映 ComfyUI sidecar 是否在线。"""
from __future__ import annotations

import bcrypt
import pytest

from src.models.api_gateway import ApiKeyGrant
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance


async def _setup(db_session):
    svc = ServiceInstance(source_type="comfy_template", source_id=1, source_name="krea2-tpl",
                          name="krea2", type="inference", status="active", category="app")
    db_session.add(svc)
    raw = "sk-cmf12345abcdef"
    key = InstanceApiKey(instance_id=None, label="t",
                         key_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
                         key_prefix=raw[:10], is_active=True)
    db_session.add(key)
    await db_session.commit()
    await db_session.refresh(svc)
    await db_session.refresh(key)
    db_session.add(ApiKeyGrant(api_key_id=key.id, service_id=svc.id, status="active"))
    await db_session.commit()
    return {"Authorization": f"Bearer {raw}"}


def _sidecar(monkeypatch, online: bool):
    from src.api.routes import _readiness

    async def _probe() -> bool:
        return online
    monkeypatch.setattr(_readiness, "comfy_sidecar_online", _probe)


@pytest.mark.asyncio
async def test_bridge_service_hidden_when_sidecar_offline(db_client, db_session, monkeypatch):
    h = await _setup(db_session)
    _sidecar(monkeypatch, False)
    ids = {m["id"] for m in (await db_client.get("/v1/models", headers=h)).json()["data"]}
    assert "krea2" not in ids
    r = await db_client.get("/v1/models?include_unready=1", headers=h)
    m = next(x for x in r.json()["data"] if x["id"] == "krea2")
    assert m["ready"] is False
    assert (await db_client.get("/v1/models/krea2", headers=h)).status_code == 503


@pytest.mark.asyncio
async def test_bridge_service_listed_when_sidecar_online(db_client, db_session, monkeypatch):
    h = await _setup(db_session)
    _sidecar(monkeypatch, True)
    ids = {m["id"] for m in (await db_client.get("/v1/models", headers=h)).json()["data"]}
    assert "krea2" in ids
    assert (await db_client.get("/v1/models/krea2", headers=h)).json()["ready"] is True


@pytest.mark.asyncio
async def test_sidecar_probe_is_cached(monkeypatch):
    """nous-app 每 30~60s 轮询,每个请求都去打 ComfyUI 没必要;短 TTL 缓存。"""
    from src.api.routes import _readiness
    from src.services.comfy import client as comfy_client

    calls = []

    class _C:
        async def health(self):
            calls.append(1)
            return {"online": True}
    monkeypatch.setattr(comfy_client, "get_comfy_client", lambda: _C())
    _readiness.reset_comfy_probe_cache()
    assert await _readiness.comfy_sidecar_online() is True
    assert await _readiness.comfy_sidecar_online() is True
    assert len(calls) == 1
    _readiness.reset_comfy_probe_cache()
    assert await _readiness.comfy_sidecar_online() is True
    assert len(calls) == 2
