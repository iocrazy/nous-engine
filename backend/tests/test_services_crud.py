"""v3 services CRUD: name regex, conflict, deprecated-still-serves.

Covers POST /api/v1/services/quick-provision + GET / PATCH / DELETE.
Auth uses the existing admin-token override pattern in conftest.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.models.service_instance import ServiceInstance
from src.models.workflow import Workflow


def _admin_headers() -> dict[str, str]:
    """ADMIN_TOKEN defaults to empty in tests, so require_admin is a no-op."""
    return {}


def _quick(name: str, **overrides):
    body = {
        "name": name,
        "category": "llm",
        "engine": "qwen3-8b",
        "label": "demo",
        "params": {},
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_quick_provision_creates_service_and_workflow(db_client, monkeypatch):
    pass  # admin auth disabled in tests
    r = await db_client.post(
        "/api/v1/services/quick-provision",
        json=_quick("nous-llm-chat"),
        headers=_admin_headers(),
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["name"] == "nous-llm-chat"
    assert data["category"] == "llm"
    assert data["meter_dim"] == "tokens"
    # The trivial workflow is back-linked to the new service.
    assert data["workflow_id"] is not None
    assert data["snapshot_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_name_regex_rejects_uppercase_and_spaces(db_client, monkeypatch):
    """The app's RequestValidationError handler maps Pydantic 422 → 400."""
    for bad in ("Foo", "with space", "1starts-digit", "x", "a" * 64):
        r = await db_client.post(
            "/api/v1/services/quick-provision",
            json=_quick(bad),
            headers=_admin_headers(),
        )
        assert r.status_code in (400, 422), (
            f"name {bad!r} should be rejected; got {r.status_code}: {r.text}"
        )


@pytest.mark.asyncio
async def test_name_collision_returns_409(db_client, monkeypatch):
    pass  # admin auth disabled in tests
    r1 = await db_client.post(
        "/api/v1/services/quick-provision",
        json=_quick("nous-dup-name"),
        headers=_admin_headers(),
    )
    assert r1.status_code == 201
    r2 = await db_client.post(
        "/api/v1/services/quick-provision",
        json=_quick("nous-dup-name"),
        headers=_admin_headers(),
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_deprecated_service_still_appears_in_list(db_client, monkeypatch):
    """`deprecated` is the warn-but-serve state; the GET /services list
    must still include it (callers paginating through statuses can filter)."""
    pass  # admin auth disabled in tests
    r = await db_client.post(
        "/api/v1/services/quick-provision",
        json=_quick("nous-aging-service"),
        headers=_admin_headers(),
    )
    assert r.status_code == 201
    sid = int(r.json()["id"])

    r2 = await db_client.patch(
        f"/api/v1/services/{sid}",
        json={"status": "deprecated"},
        headers=_admin_headers(),
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "deprecated"

    r3 = await db_client.get("/api/v1/services", headers=_admin_headers())
    assert r3.status_code == 200
    names = [s["name"] for s in r3.json()]
    assert "nous-aging-service" in names


@pytest.mark.asyncio
async def test_list_services_populates_workflow_name(db_client):
    """v3 IA: 服务卡片要标明"来自哪个 Workflow"（name + id）。
    list_services 必须 LEFT JOIN workflows 把 workflow_name 一起返。"""
    r = await db_client.post(
        "/api/v1/services/quick-provision",
        json=_quick("nous-named-svc"),
        headers=_admin_headers(),
    )
    assert r.status_code == 201
    sid = r.json()["id"]
    expected_wf_name = "trivial:nous-named-svc"  # quick_provision 的命名规则

    rs = await db_client.get("/api/v1/services", headers=_admin_headers())
    assert rs.status_code == 200
    rows = {s["id"]: s for s in rs.json()}
    assert sid in rows
    assert rows[sid]["workflow_name"] == expected_wf_name
    assert rows[sid]["workflow_id"] is not None

    rd = await db_client.get(f"/api/v1/services/{sid}", headers=_admin_headers())
    assert rd.status_code == 200
    assert rd.json()["workflow_name"] == expected_wf_name


@pytest.mark.asyncio
async def test_quick_provision_links_workflow_back(db_session):
    """The auto-generated workflow's generated_for_service_id points back
    to the service it backs (so the m08 list can group/hide them)."""
    svc = ServiceInstance(
        name="back-link-test", type="inference", status="active",
        source_type="workflow", category="llm", meter_dim="tokens",
        workflow_id=None, snapshot_hash="sha256:test",
    )
    db_session.add(svc)
    await db_session.flush()
    wf = Workflow(
        name=f"trivial:{svc.name}", auto_generated=True,
        generated_for_service_id=svc.id, status="active",
    )
    db_session.add(wf)
    await db_session.commit()

    refetched = (
        await db_session.execute(
            select(Workflow).where(Workflow.generated_for_service_id == svc.id)
        )
    ).scalar_one()
    assert refetched.auto_generated is True


@pytest.mark.asyncio
async def test_patch_rename_service(db_client):
    """改名(PATCH name):成功改 → GET 列表显新名;旧名消失。grant 靠 id 不受影响(此处只验名)。"""
    r = await db_client.post("/api/v1/services/quick-provision", json=_quick("nous-old-name"))
    assert r.status_code == 201
    sid = int(r.json()["id"])

    r2 = await db_client.patch(f"/api/v1/services/{sid}", json={"name": "nous-new-name"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["name"] == "nous-new-name"

    names = [s["name"] for s in (await db_client.get("/api/v1/services")).json()]
    assert "nous-new-name" in names and "nous-old-name" not in names


@pytest.mark.asyncio
async def test_patch_rename_invalid_format_422(db_client):
    r = await db_client.post("/api/v1/services/quick-provision", json=_quick("nous-svc-fmt"))
    sid = int(r.json()["id"])
    bad = await db_client.patch(f"/api/v1/services/{sid}", json={"name": "Bad Name!"})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_patch_rename_duplicate_409(db_client):
    a = await db_client.post("/api/v1/services/quick-provision", json=_quick("nous-svc-a"))
    await db_client.post("/api/v1/services/quick-provision", json=_quick("nous-svc-b"))
    sid_a = int(a.json()["id"])
    dup = await db_client.patch(f"/api/v1/services/{sid_a}", json={"name": "nous-svc-b"})
    assert dup.status_code == 409
