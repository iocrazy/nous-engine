"""状态页数据端点(2026-06-17,status 页 v1,admin-gated)。

`GET /api/v1/status` → 当前各组件状态(现算)+ 过去 7 天 uptime 条(聚合 status_samples)。
admin-only(用户拍板):可比公开页多露细节,且不对外暴露服务存在/健康。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from ipaddress import ip_address, ip_network

import httpx

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_admin import require_admin
from src.api.response_cache import cached
from src.models.database import get_async_session
from src.services.comfy.sidecar_status import comfy_port, tailscale_host, unit_restart_info
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


_TAILSCALE_NETWORKS = (ip_network("100.64.0.0/10"), ip_network("fd7a:115c:a1e0::/48"))


async def _probe_http(client: httpx.AsyncClient, key: str, address: str, path: str) -> dict:
    try:
        async with asyncio.timeout(3), client.stream("GET", f"{address}{path}") as response:
            code = response.status_code
    except (httpx.HTTPError, TimeoutError):
        code = None
    return {"key": key, "url": address, "http_status": code}


async def _tailscale_probes() -> list[dict] | None:
    try:
        ip = ip_address(tailscale_host())
    except ValueError:
        return None
    if ip.version == 6 and ip.scope_id is not None:
        return None
    if not any(ip in network for network in _TAILSCALE_NETWORKS):
        return None

    host = f"[{ip}]" if ip.version == 6 else str(ip)
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        return list(await asyncio.gather(
            _probe_http(client, "backend", f"http://{host}:8000", "/healthz"),
            _probe_http(client, "comfy", f"http://{host}:{comfy_port()}", "/system_stats"),
        ))


@router.get("/connections", dependencies=[Depends(require_admin)])
@cached("status-connections", ttl=10)
async def status_connections(request: Request):
    probes, comfy_unit = await asyncio.gather(_tailscale_probes(), unit_restart_info())
    return {
        "tailscale": probes,
        "comfy_unit": comfy_unit,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
