"""POST /api/v1/workflows/{id}/publish — snapshot hash, version, node validation."""

from __future__ import annotations

import pytest

from src.models.workflow import Workflow


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": "test-admin-token"}


@pytest.fixture
async def workflow_with_two_nodes(db_session):
    wf = Workflow(
        name="dag-1",
        nodes=[
            {"id": "in_1", "type": "PrimitiveInput", "data": {"value": ""}},
            {"id": "out_1", "type": "PrimitiveOutput",
             "data": {"value": ["in_1", 0]}},
        ],
        edges=[],
        status="active",
        auto_generated=False,
    )
    db_session.add(wf)
    await db_session.commit()
    await db_session.refresh(wf)
    return wf


@pytest.mark.asyncio
async def test_publish_assigns_snapshot_hash_and_version(
    db_client, workflow_with_two_nodes, monkeypatch,
):
    pass  # admin auth disabled in tests
    r = await db_client.post(
        f"/api/v1/workflows/{workflow_with_two_nodes.id}/publish",
        headers=_admin_headers(),
        json={
            "name": "nous-echo-svc",
            "label": "Echo",
            "category": "app",
            "meter_dim": "calls",
            "exposed_inputs": [
                {"node_id": "in_1", "key": "text", "input_name": "value",
                 "type": "string", "required": True},
            ],
            "exposed_outputs": [
                {"node_id": "out_1", "key": "echo", "input_name": "value",
                 "type": "string"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["name"] == "nous-echo-svc"
    assert data["version"] == 1
    assert data["snapshot_hash"].startswith("sha256:")
    assert "nodes" in data["workflow_snapshot"]


@pytest.mark.asyncio
async def test_publish_flips_workflow_status_to_published(
    db_client, db_session, workflow_with_two_nodes,
):
    """publish 必须把 wf.status 翻成 published —— 否则 main.py 启动只对 published 工作流
    re-register 模型引用,重启后已发布服务掉引用、模型可被卸载(bug hunt round2 #4)。"""
    r = await db_client.post(
        f"/api/v1/workflows/{workflow_with_two_nodes.id}/publish",
        headers=_admin_headers(),
        json={
            "name": "nous-status-svc", "label": "S", "category": "app", "meter_dim": "calls",
            "exposed_inputs": [{"node_id": "in_1", "key": "text", "input_name": "v",
                                "type": "string", "required": True}],
            "exposed_outputs": [{"node_id": "out_1", "key": "echo", "input_name": "v",
                                 "type": "string"}],
        },
    )
    assert r.status_code == 201, r.text
    await db_session.refresh(workflow_with_two_nodes)
    assert workflow_with_two_nodes.status == "published"


@pytest.mark.asyncio
async def test_delete_service_resets_workflow_status_to_draft(
    db_client, db_session, workflow_with_two_nodes,
):
    """删服务(源工作流已无其它关联服务)→ wf.status 退回 draft。镜像 publish 的状态机。
    之前删服务没回退 → 工作流卡在 published 但无关联服务(卡片绿徽章与"未关联服务"+
    发布按钮冲突,用户反馈)。"""
    pub = await db_client.post(
        f"/api/v1/workflows/{workflow_with_two_nodes.id}/publish",
        headers=_admin_headers(),
        json={
            "name": "nous-del-svc", "label": "D", "category": "app", "meter_dim": "calls",
            "exposed_inputs": [{"node_id": "in_1", "key": "text", "input_name": "v",
                                "type": "string", "required": True}],
            "exposed_outputs": [{"node_id": "out_1", "key": "echo", "input_name": "v",
                                 "type": "string"}],
        },
    )
    assert pub.status_code == 201, pub.text
    await db_session.refresh(workflow_with_two_nodes)
    assert workflow_with_two_nodes.status == "published"
    svc_id = pub.json()["id"]

    r = await db_client.delete(f"/api/v1/services/{svc_id}", headers=_admin_headers())
    assert r.status_code == 204, r.text
    await db_session.refresh(workflow_with_two_nodes)
    assert workflow_with_two_nodes.status == "draft"


@pytest.mark.asyncio
async def test_publish_rejects_unknown_exposed_node_id(
    db_client, workflow_with_two_nodes, monkeypatch,
):
    """The plan mandates 422 when an exposed.node_id doesn't resolve in
    the snapshot — silent acceptance would route caller payloads into a void."""
    pass  # admin auth disabled in tests
    r = await db_client.post(
        f"/api/v1/workflows/{workflow_with_two_nodes.id}/publish",
        headers=_admin_headers(),
        json={
            "name": "nous-broken-svc",
            "exposed_inputs": [
                {"node_id": "ghost-node", "key": "x", "input_name": "value",
                 "type": "string", "required": True},
            ],
            "exposed_outputs": [],
        },
    )
    assert r.status_code == 422
    assert "ghost-node" in r.text


@pytest.mark.asyncio
async def test_publish_rejects_auto_generated_workflow(db_client, db_session, monkeypatch):
    """Quick-provisioned trivial workflows are owned by their service;
    re-publishing one would create an unbacked fork."""
    pass  # admin auth disabled in tests
    wf = Workflow(
        name="trivial:foo", auto_generated=True, status="active",
        nodes=[{"id": "n1", "type": "PrimitiveInput"}],
    )
    db_session.add(wf)
    await db_session.commit()
    await db_session.refresh(wf)

    r = await db_client.post(
        f"/api/v1/workflows/{wf.id}/publish",
        headers=_admin_headers(),
        json={"name": "nous-fork", "exposed_inputs": [], "exposed_outputs": []},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_publish_409_on_duplicate_name(
    db_client, workflow_with_two_nodes, monkeypatch,
):
    pass  # admin auth disabled in tests
    base = {
        "exposed_inputs": [], "exposed_outputs": [],
    }
    r1 = await db_client.post(
        f"/api/v1/workflows/{workflow_with_two_nodes.id}/publish",
        headers=_admin_headers(), json={**base, "name": "nous-dup-svc"},
    )
    assert r1.status_code == 201
    r2 = await db_client.post(
        f"/api/v1/workflows/{workflow_with_two_nodes.id}/publish",
        headers=_admin_headers(), json={**base, "name": "nous-dup-svc"},
    )
    assert r2.status_code == 409


def test_snapshot_hash_is_stable_across_key_order():
    """sort_keys=True in the hash function: the same dict, serialized in
    different field order, must produce the same digest."""
    from src.api.routes.services import _snapshot_hash
    a = {"nodes": {"a": {"x": 1, "y": 2}}, "schema": "comfy/api-1"}
    b = {"schema": "comfy/api-1", "nodes": {"a": {"y": 2, "x": 1}}}
    assert _snapshot_hash(a) == _snapshot_hash(b)
