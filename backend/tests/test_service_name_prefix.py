"""服务名必须以 `nous-` 开头(2026-09-27 用户决定)。

规则 `^nous-[a-z0-9][a-z0-9-]{0,57}$`(总长 ≤63),只作用于写路径:
quick-provision / register-model / publish / comfy 模板导入 / 改名 PATCH。
pydantic body 校验错误被全局 handler 转成 400;PATCH 改名路径直接抛 422。
"""

from __future__ import annotations

import pytest

from src.models.workflow import Workflow
from src.services.workflow_snapshot import NAME_RE

REJECTED = ("foo-bar", "nous-", "nous", "Nous-x", "nous-Foo", "nous--x", "nous-" + "a" * 59)
ACCEPTED = ("nous-foo-bar", "nous-x", "nous-0", "nous-" + "a" * 58)

_COMFY_WF = {"92": {"class_type": "SaveVideo", "inputs": {}}}


def _quick(name: str) -> dict:
    return {"name": name, "category": "llm", "engine": "qwen3-8b", "label": "", "params": {}}


def _register(name: str) -> dict:
    return {"name": name, "source_name": "qwen3_embedding_8b", "type": "embedding"}


def _publish(name: str) -> dict:
    return {
        "name": name, "label": "P", "category": "app", "meter_dim": "calls",
        "exposed_inputs": [{"node_id": "in_1", "key": "text", "input_name": "value",
                            "type": "string", "required": True}],
        "exposed_outputs": [{"node_id": "out_1", "key": "echo", "input_name": "value",
                             "type": "string"}],
    }


def _assert_rejected(r, name: str) -> None:
    assert r.status_code in (400, 422), f"{name!r} should be rejected: {r.status_code} {r.text}"
    assert "nous-" in r.text, r.text


def test_name_re_boundaries():
    assert len("nous-" + "a" * 58) == 63
    for name in ACCEPTED:
        assert NAME_RE.match(name), name
    for name in REJECTED:
        assert not NAME_RE.match(name), name


@pytest.fixture
async def workflow_id(db_session) -> int:
    wf = Workflow(
        name="dag-prefix",
        nodes=[
            {"id": "in_1", "type": "PrimitiveInput", "data": {"value": ""}},
            {"id": "out_1", "type": "PrimitiveOutput", "data": {"value": ["in_1", 0]}},
        ],
        edges=[], status="active", auto_generated=False,
    )
    db_session.add(wf)
    await db_session.commit()
    await db_session.refresh(wf)
    return wf.id


@pytest.mark.asyncio
@pytest.mark.parametrize("name", REJECTED)
async def test_quick_provision_rejects_unprefixed(db_client, name):
    r = await db_client.post("/api/v1/services/quick-provision", json=_quick(name))
    _assert_rejected(r, name)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ("nous-foo-bar", "nous-" + "a" * 58))
async def test_quick_provision_accepts_prefixed(db_client, name):
    r = await db_client.post("/api/v1/services/quick-provision", json=_quick(name))
    assert r.status_code == 201, r.text
    assert r.json()["name"] == name


@pytest.mark.asyncio
async def test_register_model_prefix(db_client):
    bad = await db_client.post("/api/v1/services/register-model", json=_register("foo-bar"))
    _assert_rejected(bad, "foo-bar")
    ok = await db_client.post("/api/v1/services/register-model", json=_register("nous-foo-bar"))
    assert ok.status_code == 201, ok.text


@pytest.mark.asyncio
async def test_publish_prefix(db_client, workflow_id):
    url = f"/api/v1/workflows/{workflow_id}/publish"
    for name in ("foo-bar", "nous-", "Nous-x"):
        _assert_rejected(await db_client.post(url, json=_publish(name)), name)
    ok = await db_client.post(url, json=_publish("nous-foo-bar"))
    assert ok.status_code == 201, ok.text


@pytest.mark.asyncio
async def test_comfy_import_prefix(client):
    for name in ("foo-bar", "nous", "nous-" + "a" * 59):
        r = await client.post("/api/v1/comfy-templates", json={"name": name, "workflow": _COMFY_WF})
        _assert_rejected(r, name)
    ok = await client.post(
        "/api/v1/comfy-templates", json={"name": "nous-foo-bar", "workflow": _COMFY_WF},
    )
    assert ok.status_code == 201, ok.text


@pytest.mark.asyncio
async def test_rename_prefix(db_client):
    r = await db_client.post("/api/v1/services/quick-provision", json=_quick("nous-orig"))
    assert r.status_code == 201, r.text
    sid = int(r.json()["id"])
    for name in ("foo-bar", "nous-", "nous", "Nous-x", "nous-" + "a" * 59):
        bad = await db_client.patch(f"/api/v1/services/{sid}", json={"name": name})
        assert bad.status_code == 422, f"{name!r}: {bad.status_code} {bad.text}"
        assert "nous-" in bad.text
    ok = await db_client.patch(f"/api/v1/services/{sid}", json={"name": "nous-" + "b" * 58})
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == "nous-" + "b" * 58
