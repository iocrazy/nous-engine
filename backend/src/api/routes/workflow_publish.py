"""POST /api/v1/workflows/{id}/publish — freeze a workflow into a service.

The publish flow is the second creation path for a service (the first
being quick-provision in routes/services.py). It snapshots the source
workflow and stores it on a fresh `service_instances` row, with the
caller-supplied `exposed_inputs` / `exposed_outputs` defining the
external schema.

Per the v3 plan we MUST validate that every `exposed.node_id` referenced
by the schema actually exists in the snapshot — silently shipping a
service whose schema points at deleted nodes would route caller payloads
into a void.
"""

from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_admin import require_admin
from src.api.response_cache import invalidate
from src.api.routes.services import ServiceDetailOut
from src.models.database import get_async_session
from src.models.schemas import ExposedParam
from src.models.service_instance import ServiceInstance
from src.models.workflow import Workflow
from src.services.workflow_snapshot import (
    _IMAGE_NODE_TYPES,
    _IMAGE_OUTPUT_FIELDS,
    _METER_DIM_BY_CATEGORY,
    _build_snapshot,
    _detect_category,
    _node_ids,
    _node_types_by_id,
    _snapshot_hash,
    NAME_RE,
    NAME_RULE_MSG,
)

router = APIRouter(prefix="/api/v1", tags=["workflow-publish"])

# 快照/类别的纯逻辑(_snapshot_hash / _node_ids / _detect_category / _build_snapshot
# / _IMAGE_* / NAME_RE / _METER_DIM_BY_CATEGORY)已移到 services.workflow_snapshot,
# 打破本文件 ↔ services.py 的循环依赖(顶部 import)。


async def reconcile_service_categories(session: AsyncSession) -> int:
    """Re-derive category/meter_dim for workflow-sourced services from their
    frozen snapshot, fixing rows that predate a detector improvement.

    Idempotent and self-healing: `_detect_category` only ever returns "image"
    or None, so this upgrades misfiled image services (frozen as "app"/"calls"
    when published through the integrated image_generate node, before detection
    keyed off the image_output sink) WITHOUT clobbering an explicitly-locked
    llm/tts/vl category. Returns the number of rows changed. Caller owns the
    commit + cache invalidation so this stays unit-testable against a session.
    """
    from sqlalchemy.orm import undefer

    stmt = (
        select(ServiceInstance)
        .where(ServiceInstance.source_type == "workflow")
        .options(undefer(ServiceInstance.workflow_snapshot))
    )
    changed = 0
    for svc in (await session.execute(stmt)).scalars():
        detected = _detect_category(svc.workflow_snapshot or {})
        if detected and svc.category != detected:
            svc.category = detected
            svc.meter_dim = _METER_DIM_BY_CATEGORY.get(detected, svc.meter_dim)
            changed += 1
    return changed


class PublishBody(BaseModel):
    name: str
    label: str = ""
    category: str | None = None  # llm|tts|vl|app
    meter_dim: str | None = None
    exposed_inputs: list[ExposedParam] = []
    exposed_outputs: list[ExposedParam] = []

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(NAME_RULE_MSG)
        return v


@router.post(
    "/workflows/{workflow_id}/publish",
    response_model=ServiceDetailOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
async def publish_workflow(
    workflow_id: int,
    body: PublishBody,
    session: AsyncSession = Depends(get_async_session),
):
    wf = await session.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(404, detail="workflow not found")
    if wf.auto_generated:
        # Trivial workflows are owned by their service. Re-publishing one
        # would create a fork that contradicts "service = published wf".
        raise HTTPException(
            409,
            detail="cannot publish an auto-generated workflow; edit the source instead",
        )

    snapshot = _build_snapshot(wf)
    valid_ids = _node_ids(snapshot)
    types_by_id = _node_types_by_id(snapshot)

    # Hard contract: every exposed.node_id MUST resolve.
    for kind, params in (("input", body.exposed_inputs), ("output", body.exposed_outputs)):
        for p in params:
            if str(p.node_id) not in valid_ids:
                raise HTTPException(
                    422,
                    detail=(
                        f"exposed {kind} references node_id {p.node_id!r} "
                        f"that does not exist in the workflow snapshot"
                    ),
                )

    # Image envelope guard: a typo in input_name ("image_uri" vs
    # "image_url") would silently publish a service whose response payload
    # is `null`. Reject at publish time so the caller sees the mistake
    # before any downstream consumer integrates against it.
    for p in body.exposed_outputs:
        node_type = types_by_id.get(str(p.node_id))
        if node_type not in _IMAGE_NODE_TYPES:
            continue
        field = p.input_name
        if field and field not in _IMAGE_OUTPUT_FIELDS:
            raise HTTPException(
                422,
                detail=(
                    f"exposed output input_name={field!r} is not emitted by "
                    f"{node_type}; allowed: "
                    f"{sorted(_IMAGE_OUTPUT_FIELDS)}"
                ),
            )

    # Name collision: bump version on existing-name re-publish? v3 says
    # "禁止重命名，只允许 deprecate + new name". So we 409 on name collision
    # — callers are expected to either pick a new name or PATCH the old
    # service to `deprecated` first.
    existing = await session.scalar(
        select(ServiceInstance).where(ServiceInstance.name == body.name)
    )
    if existing is not None:
        raise HTTPException(409, detail=f"service name '{body.name}' already exists")

    snapshot_hash = _snapshot_hash(snapshot)

    # When the caller doesn't lock the category, infer from snapshot
    # contents so meter_dim picks up the right unit (image → "images").
    detected_category = _detect_category(snapshot)
    resolved_category = body.category or detected_category or "app"
    resolved_meter = (
        body.meter_dim
        or _METER_DIM_BY_CATEGORY.get(resolved_category)
        or "calls"
    )

    svc = ServiceInstance(
        name=body.name,
        type="inference",
        status="active",
        source_type="workflow",
        source_id=workflow_id,
        category=resolved_category,
        meter_dim=resolved_meter,
        workflow_id=workflow_id,
        workflow_snapshot=snapshot,
        exposed_inputs=[p.model_dump(exclude_none=True) for p in body.exposed_inputs],
        exposed_outputs=[p.model_dump(exclude_none=True) for p in body.exposed_outputs],
        snapshot_hash=snapshot_hash,
        snapshot_schema_version=1,
        version=1,
    )
    session.add(svc)
    # 真的把 wf.status 翻成 "published"(注释一直说会翻、代码漏了)。main.py 启动时只对
    # status="published" 的 workflow re-register 模型引用 —— 不翻则重启后已发布服务掉引用、
    # 模型可被 idle/LRU 卸载(bug hunt round2 #4)。与 unpublish 写回 "draft" 形成完整状态机。
    wf.status = "published"
    await session.commit()
    await session.refresh(
        svc,
        attribute_names=["workflow_snapshot", "exposed_inputs", "exposed_outputs"],
    )
    # Publish creates a service AND flips workflow.status="published" downstream;
    # both list caches must drop.
    invalidate("services", "workflows")
    return svc
