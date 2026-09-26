"""状态页数据端点(2026-06-17,status 页 v1,admin-gated)。

`GET /api/v1/status` → 当前各组件状态(现算)+ 过去 7 天 uptime 条(聚合 status_samples)。
admin-only(用户拍板):可比公开页多露细节,且不对外暴露服务存在/健康。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_admin import require_admin
from src.api.response_cache import cached
from src.models.database import get_async_session
from src.services.status_sampler import (
    COMPONENTS,
    compute_statuses,
    uptime_history,
    worst,
)

router = APIRouter(prefix="/api/v1/status", tags=["status"])


@router.get("", dependencies=[Depends(require_admin)])
@cached("status", ttl=10)
async def status_snapshot(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    details: dict[str, str] = {}
    current = await compute_statuses(request.app.state, session, details)
    history = await uptime_history(session, days=7)
    components = []
    for key, name in COMPONENTS:
        hist = history.get(key, {})
        components.append({
            "key": key,
            "name": name,
            "status": current.get(key, "down"),
            "uptime_7d": hist.get("uptime_pct"),
            "days": hist.get("days", []),
            "detail": details.get(key),
        })
    return {
        "overall": worst(current.values()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "components": components,
    }
