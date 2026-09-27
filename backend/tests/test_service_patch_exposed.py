"""PATCH /api/v1/services/{id} — edit exposed_inputs/outputs in place.

服务页「应用编辑」tab 的落库路径(spec 2026-06-09 PR-1)。改的是对外 schema 映射,
不动 snapshot 本体;node_id 必须能在 frozen snapshot 里解析,否则 422(与 publish
同契约)。
"""

from __future__ import annotations

import pytest

from src.models.workflow import Workflow


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": "test-admin-token"}


@pytest.fixture
async def published_service_id(db_client, db_session):
    wf = Workflow(
        name="patch-wf",
        nodes=[
            {"id": "in_1", "type": "text_input", "data": {"text": ""}},
            {"id": "out_1", "type": "text_output", "data": {"text": ["in_1", 0]}},
        ],
        edges=[],
        status="active",
        auto_generated=False,
    )
    db_session.add(wf)
    await db_session.commit()
    await db_session.refresh(wf)
    r = await db_client.post(
        f"/api/v1/workflows/{wf.id}/publish",
        headers=_admin_headers(),
        json={
            "name": "nous-patch-svc",
            "category": "app",
            "exposed_inputs": [
                {"node_id": "in_1", "key": "text", "input_name": "text",
                 "type": "string", "required": True},
            ],
            "exposed_outputs": [
                {"node_id": "out_1", "key": "out", "input_name": "text",
                 "type": "string"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_patch_exposed_inputs_persists(db_client, published_service_id):
    sid = published_service_id
    r = await db_client.patch(
        f"/api/v1/services/{sid}",
        headers=_admin_headers(),
        json={
            "exposed_inputs": [
                {"node_id": "in_1", "key": "prompt", "input_name": "text",
                 "type": "string", "label": "提示词", "required": True,
                 "constraints": {"format": "single_line"}},
            ],
        },
    )
    assert r.status_code == 200, r.text

    detail = (
        await db_client.get(f"/api/v1/services/{sid}", headers=_admin_headers())
    ).json()
    assert [p["key"] for p in detail["exposed_inputs"]] == ["prompt"]
    assert detail["exposed_inputs"][0]["label"] == "提示词"
    assert detail["exposed_inputs"][0]["constraints"] == {"format": "single_line"}
    # outputs omitted in patch (None) → left untouched
    assert len(detail["exposed_outputs"]) == 1
    assert detail["exposed_outputs"][0]["key"] == "out"


@pytest.mark.asyncio
async def test_patch_exposed_rejects_unknown_node_id(db_client, published_service_id):
    sid = published_service_id
    r = await db_client.patch(
        f"/api/v1/services/{sid}",
        headers=_admin_headers(),
        json={
            "exposed_inputs": [
                {"node_id": "ghost", "key": "x", "input_name": "text",
                 "type": "string"},
            ],
        },
    )
    assert r.status_code == 422, r.text
    assert "does not exist" in r.text


@pytest.mark.asyncio
async def test_patch_status_only_leaves_exposed_intact(db_client, published_service_id):
    sid = published_service_id
    r = await db_client.patch(
        f"/api/v1/services/{sid}",
        headers=_admin_headers(),
        json={"status": "paused"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paused"

    detail = (
        await db_client.get(f"/api/v1/services/{sid}", headers=_admin_headers())
    ).json()
    # status-only patch must not wipe the exposed schema
    assert len(detail["exposed_inputs"]) == 1
    assert len(detail["exposed_outputs"]) == 1
