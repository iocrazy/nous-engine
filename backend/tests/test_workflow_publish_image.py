"""PR-7: image workflow publish closure.

- Auto-detects category="image" + meter_dim="images" from snapshot nodes
- Rejects exposed_outputs whose input_name isn't in the image_generate envelope
- Explicit body.category overrides the heuristic
"""
from __future__ import annotations

import pytest

from src.models.workflow import Workflow


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": "test-admin-token"}


@pytest.fixture
async def image_workflow(db_session):
    """细粒度图 image DAG —— 收敛后终端是 flux2_vae_decode(image_generate 已删)。
    publish 的 category 检测 + exposed_outputs 校验 key 在 flux2_vae_decode 上。"""
    wf = Workflow(
        name="img-flow",
        nodes=[
            {"id": "in_1", "type": "text_input", "data": {"text": "a cat"}},
            {"id": "img_1", "type": "flux2_vae_decode",
             "data": {"width": 512, "height": 512}},
            {"id": "out_1", "type": "image_output", "data": {}},
        ],
        edges=[
            {"id": "e1", "source": "in_1", "sourceHandle": "text",
             "target": "img_1", "targetHandle": "prompt"},
            {"id": "e2", "source": "img_1", "sourceHandle": "image",
             "target": "out_1", "targetHandle": "image"},
        ],
        status="active",
        auto_generated=False,
    )
    db_session.add(wf)
    await db_session.commit()
    await db_session.refresh(wf)
    return wf


@pytest.mark.asyncio
async def test_publish_image_workflow_auto_detects_category_and_meter(
    db_client, image_workflow,
):
    r = await db_client.post(
        f"/api/v1/workflows/{image_workflow.id}/publish",
        headers=_admin_headers(),
        json={
            "name": "nous-image-svc",
            "label": "Image",
            # Note: NO category supplied — must be inferred from snapshot.
            "exposed_inputs": [
                {"node_id": "in_1", "key": "prompt", "input_name": "text",
                 "type": "string", "required": True},
            ],
            "exposed_outputs": [
                {"node_id": "img_1", "key": "url", "input_name": "image_url",
                 "type": "string"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["category"] == "image"
    assert data["meter_dim"] == "images"


@pytest.mark.asyncio
async def test_publish_image_workflow_explicit_category_wins(
    db_client, image_workflow,
):
    r = await db_client.post(
        f"/api/v1/workflows/{image_workflow.id}/publish",
        headers=_admin_headers(),
        json={
            "name": "nous-image-svc-2",
            "category": "app",  # admin override
            "meter_dim": "calls",
            "exposed_inputs": [],
            "exposed_outputs": [
                {"node_id": "img_1", "key": "url", "input_name": "image_url",
                 "type": "string"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["category"] == "app"
    assert data["meter_dim"] == "calls"


@pytest.mark.asyncio
async def test_publish_image_workflow_rejects_bad_output_field(
    db_client, image_workflow,
):
    """Typo in the envelope field name (image_uri vs image_url) would
    silently publish a service whose payload is null. Must 422 at publish."""
    r = await db_client.post(
        f"/api/v1/workflows/{image_workflow.id}/publish",
        headers=_admin_headers(),
        json={
            "name": "nous-image-svc-bad-output",
            "exposed_outputs": [
                {"node_id": "img_1", "key": "url", "input_name": "image_uri",
                 "type": "string"},
            ],
        },
    )
    assert r.status_code == 422
    msg = r.json().get("error", {}).get("message", "")
    assert "image_uri" in msg
    assert "image_url" in msg  # the allowed list is mentioned in the error


@pytest.mark.asyncio
async def test_publish_image_workflow_allows_alt_output_fields(
    db_client, image_workflow,
):
    """All envelope fields image_generate emits should publish without 422."""
    for field in ["image_url", "image_uuid", "width", "seed", "duration_ms"]:
        slug = field.replace("_", "-")
        r = await db_client.post(
            f"/api/v1/workflows/{image_workflow.id}/publish",
            headers=_admin_headers(),
            json={
                "name": f"nous-image-svc-{slug}",
                "exposed_outputs": [
                    {"node_id": "img_1", "key": "x", "input_name": field,
                     "type": "string"},
                ],
            },
        )
        assert r.status_code == 201, f"field={field} failed: {r.text}"


@pytest.mark.asyncio
async def test_publish_non_image_workflow_does_not_get_image_meter(
    db_client, db_session,
):
    """text-only workflow → no auto-detection kicks in, falls back to defaults."""
    wf = Workflow(
        name="text-flow",
        nodes=[
            {"id": "in_1", "type": "text_input", "data": {"text": "hi"}},
            {"id": "out_1", "type": "text_output", "data": {}},
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
            "name": "nous-text-svc",
            "exposed_inputs": [
                {"node_id": "in_1", "key": "text", "input_name": "text",
                 "type": "string", "required": True},
            ],
            "exposed_outputs": [],
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["category"] == "app"  # falls back to default
    assert data["meter_dim"] == "calls"


@pytest.mark.asyncio
async def test_publish_unknown_node_in_image_workflow_still_422(
    db_client, image_workflow,
):
    """The image schema check shouldn't bypass the existing node_id
    existence check — typo in node_id still 422 at the original gate."""
    r = await db_client.post(
        f"/api/v1/workflows/{image_workflow.id}/publish",
        headers=_admin_headers(),
        json={
            "name": "nous-image-svc-ghost",
            "exposed_outputs": [
                {"node_id": "ghost", "key": "url", "input_name": "image_url",
                 "type": "string"},
            ],
        },
    )
    assert r.status_code == 422
    msg = r.json().get("error", {}).get("message", "")
    assert "ghost" in msg


@pytest.mark.asyncio
async def test_reconcile_backfills_misfiled_image_service(db_session):
    """Startup backfill: an image service frozen as category="app" +
    meter_dim="calls" (published via image_generate before sink detection)
    is re-derived to image/images. This is the billing fix — image gens were
    metered as generic calls."""
    from src.models.service_instance import ServiceInstance
    from src.api.routes.workflow_publish import reconcile_service_categories

    svc = ServiceInstance(
        name="legacy-img-svc",
        type="inference",
        status="active",
        source_type="workflow",
        category="app",          # misfiled
        meter_dim="calls",       # mis-metered
        workflow_id=123,
        workflow_snapshot={
            "schema": "comfy/api-1",
            "nodes": {
                "gen": {"class_type": "image_generate",
                        "inputs": {"model_key": "flux2-klein-9b-true-v2-fp8mixed"}},
                "out": {"class_type": "image_output", "inputs": {}},
            },
        },
    )
    db_session.add(svc)
    await db_session.commit()

    changed = await reconcile_service_categories(db_session)
    await db_session.commit()
    await db_session.refresh(svc)

    assert changed == 1
    assert svc.category == "image"
    assert svc.meter_dim == "images"


@pytest.mark.asyncio
async def test_reconcile_leaves_non_image_services_untouched(db_session):
    """The backfill must never clobber an explicitly-locked llm/tts/vl
    category — _detect_category returns None for non-image snapshots, so
    those rows are skipped (changed count excludes them)."""
    from src.models.service_instance import ServiceInstance
    from src.api.routes.workflow_publish import reconcile_service_categories

    svc = ServiceInstance(
        name="text-svc-locked",
        type="inference",
        status="active",
        source_type="workflow",
        category="llm",
        meter_dim="tokens",
        workflow_id=456,
        workflow_snapshot={
            "schema": "comfy/api-1",
            "nodes": {
                "llm": {"class_type": "llm", "inputs": {"model_key": "qwen3-8b"}},
                "out": {"class_type": "text_output", "inputs": {}},
            },
        },
    )
    db_session.add(svc)
    await db_session.commit()

    changed = await reconcile_service_categories(db_session)
    await db_session.refresh(svc)

    assert changed == 0
    assert svc.category == "llm"
    assert svc.meter_dim == "tokens"


def test_meter_dim_lookup_includes_image():
    """services.py exposes the canonical category → meter mapping; image
    must appear so quick-provision and publish stay in sync."""
    from src.api.routes.services import _METER_DIM_BY_CATEGORY
    assert _METER_DIM_BY_CATEGORY["image"] == "images"


def test_detect_category_from_snapshot():
    from src.api.routes.workflow_publish import _detect_category

    img_snap = {
        "schema": "comfy/api-1",
        "nodes": {
            "a": {"class_type": "text_input", "inputs": {}},
            "b": {"class_type": "flux2_vae_decode", "inputs": {}},
        },
    }
    text_snap = {
        "schema": "comfy/api-1",
        "nodes": {"a": {"class_type": "llm", "inputs": {}}},
    }
    assert _detect_category(img_snap) == "image"
    assert _detect_category(text_snap) is None


def test_detect_category_recognises_image_generate_legacy_node():
    """Regression: services published through the integrated image_generate
    node have no flux2_vae_decode terminus. The old single-element detection
    set missed them → froze as category="app" + meter_dim="calls" (misfiled
    in the UI AND mis-metered as generic calls instead of images)."""
    from src.api.routes.workflow_publish import _detect_category

    legacy_snap = {
        "schema": "comfy/api-1",
        "nodes": {
            "in": {"class_type": "text_input", "inputs": {}},
            "gen": {"class_type": "image_generate",
                    "inputs": {"model_key": "flux2-klein-9b-true-v2-fp8mixed"}},
            "out": {"class_type": "image_output", "inputs": {}},
        },
    }
    assert _detect_category(legacy_snap) == "image"


def test_detect_category_keys_off_image_output_sink():
    """The robust, producer-agnostic signal is the image_output sink node —
    every image flow ends in it regardless of which engine produced the image.
    A snapshot with only a sink (no recognized producer) still detects image."""
    from src.api.routes.workflow_publish import _detect_category

    sink_only = {
        "schema": "comfy/api-1",
        "nodes": {
            "in": {"class_type": "text_input", "inputs": {}},
            "out": {"class_type": "image_output", "inputs": {}},
        },
    }
    assert _detect_category(sink_only) == "image"


def test_detect_category_recognises_flux2_vae_decode_terminus():
    """V1' Lane C component workflows end on flux2_vae_decode (the
    counterpart to image_generate's integrated terminus). Both paths
    must auto-tag the published service as category=image so quick-
    provision wires up the image meter automatically."""
    from src.api.routes.workflow_publish import _detect_category

    component_snap = {
        "schema": "comfy/api-1",
        "nodes": {
            "n1": {"class_type": "flux2_load_checkpoint", "inputs": {}},
            "n2": {"class_type": "flux2_encode_prompt", "inputs": {}},
            "n3": {"class_type": "flux2_ksampler", "inputs": {}},
            "n4": {"class_type": "flux2_vae_decode", "inputs": {}},
        },
    }
    assert _detect_category(component_snap) == "image"


@pytest.fixture
async def component_image_workflow(db_session):
    """V1' Lane C component workflow:
    text_input → LoadCheckpoint → EncodePrompt → KSampler → VAEDecode → image_output."""
    wf = Workflow(
        name="component-img-flow",
        nodes=[
            {"id": "in_1",   "type": "text_input",                "data": {"text": "a cat"}},
            {"id": "load",   "type": "flux2_load_checkpoint",
             "data": {"model_key": "flux2-klein-9b-true-v2-fp8mixed"}},
            {"id": "enc",    "type": "flux2_encode_prompt",       "data": {}},
            {"id": "ksm",    "type": "flux2_ksampler",            "data": {"width": 512, "height": 512}},
            {"id": "dec",    "type": "flux2_vae_decode",          "data": {}},
            {"id": "out_1",  "type": "image_output",              "data": {}},
        ],
        edges=[
            {"id": "e1", "source": "in_1",  "sourceHandle": "text",
             "target": "enc",   "targetHandle": "text"},
            {"id": "e2", "source": "load",  "sourceHandle": "clip",
             "target": "enc",   "targetHandle": "clip"},
            {"id": "e3", "source": "load",  "sourceHandle": "model",
             "target": "ksm",   "targetHandle": "model"},
            {"id": "e4", "source": "enc",   "sourceHandle": "conditioning",
             "target": "ksm",   "targetHandle": "conditioning"},
            {"id": "e5", "source": "load",  "sourceHandle": "vae",
             "target": "dec",   "targetHandle": "vae"},
            {"id": "e6", "source": "ksm",   "sourceHandle": "latent",
             "target": "dec",   "targetHandle": "latent"},
            {"id": "e7", "source": "dec",   "sourceHandle": "image",
             "target": "out_1", "targetHandle": "image"},
        ],
        status="active",
        auto_generated=False,
    )
    db_session.add(wf)
    await db_session.commit()
    await db_session.refresh(wf)
    return wf


@pytest.mark.asyncio
async def test_publish_flux2_component_workflow_auto_detects_image_category(
    db_client, component_image_workflow,
):
    """The Lane C component path terminates on flux2_vae_decode rather
    than image_generate. _detect_category must pick that up so quick-
    provision wires the image meter without an explicit category in
    the publish body."""
    r = await db_client.post(
        f"/api/v1/workflows/{component_image_workflow.id}/publish",
        headers=_admin_headers(),
        json={
            "name": "nous-component-img-svc",
            "label": "Component Image",
            "exposed_inputs": [],
            "exposed_outputs": [
                {"name": "image_url", "node_id": "dec", "input_name": "image_url"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["category"] == "image"


@pytest.mark.asyncio
async def test_publish_component_workflow_rejects_typo_in_vae_decode_output(
    db_client, component_image_workflow,
):
    """exposed_outputs pointing at a flux2_vae_decode node must use a
    canonical image-envelope field. Typos 422 at publish time so the
    consumer doesn't get null from the service."""
    r = await db_client.post(
        f"/api/v1/workflows/{component_image_workflow.id}/publish",
        headers=_admin_headers(),
        json={
            "name": "nous-component-img-bad",
            "exposed_inputs": [],
            "exposed_outputs": [
                {"name": "img", "node_id": "dec", "input_name": "image_uri"},
            ],
        },
    )
    assert r.status_code == 422
    assert "image_uri" in r.text
    assert "flux2_vae_decode" in r.text
