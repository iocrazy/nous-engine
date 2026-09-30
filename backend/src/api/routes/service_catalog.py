"""公开服务目录 `GET /v1/services`(服务发现,2026-09-29)。

调用方(nous-app)不认服务名也能拿到「有哪些文生图 / 图像编辑 / 图片放大服务」:只列
声明了 `discovery` 的 active 服务,每项是 `discovery_view` 的公开字段 + 服务名 +
`schema_url`。具体参数仍以 `/v1/services/{name}/schema` 为唯一真相 —— 这里不复制 schema。

鉴权同 predictions:M:N key 只看得到自己被授权的服务;admin session / ADMIN_TOKEN 看全部。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from src.api.service_access import Auth, auth_bearer_or_admin_session
from src.models.api_gateway import ApiKeyGrant
from src.models.database import get_async_session
from src.models.service_instance import ServiceInstance
from src.services.service_discovery import Operation, discovery_view
from src.services.service_schema import build_service_io_schema

router = APIRouter(prefix="/v1", tags=["service-catalog"])


@router.get("/services")
async def list_discoverable_services(
    operation: Operation | None = None,
    auth: Auth = Depends(auth_bearer_or_admin_session),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    _, api_key = auth
    stmt = (
        select(ServiceInstance)
        .options(
            undefer(ServiceInstance.workflow_snapshot),
            undefer(ServiceInstance.exposed_inputs),
            undefer(ServiceInstance.exposed_outputs),
        )
        .where(ServiceInstance.status == "active")
        .where(ServiceInstance.discovery.is_not(None))
        .order_by(ServiceInstance.name)
    )
    if api_key is not None:
        stmt = stmt.join(ApiKeyGrant, ApiKeyGrant.service_id == ServiceInstance.id).where(
            ApiKeyGrant.api_key_id == api_key.id, ApiKeyGrant.status == "active")
    data = []
    for svc in (await session.execute(stmt)).scalars():
        schema = build_service_io_schema(
            svc.exposed_inputs, svc.exposed_outputs, svc.workflow_snapshot)
        view = discovery_view(svc.discovery, svc.exposed_inputs, schema["input_schema"])
        if view is None or (operation is not None and view["operation"] != operation):
            continue
        data.append({
            "service": svc.name,
            **view,
            "outputs": list(schema["output_schema"].get("properties") or {}),
            "schema_url": f"/v1/services/{svc.name}/schema",
        })
    return {"object": "list", "data": data}
