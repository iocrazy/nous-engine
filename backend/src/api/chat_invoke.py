"""非流式 chat 调用的共用核心(spec 2026-09-26 skill-runs §4)。

从 openai_compat.chat_completions 抽出,`/v1/chat/completions` 与 `/v1/skill-runs/preview` 共用:
readiness(未加载即 503,绝不在请求路径上加载)、引擎引用护栏(C3)、调 vLLM、用量记录、
配额扣减 —— 各只此一份实现。放在 src/api/ 而非 src/services/:这里抛 HTTP 错误、要读
routes/_readiness,放服务层就是服务层反向 import API 层。
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.errors import ModelNotReadyError
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance
from src.services.inference.vllm_endpoint import (
    VLLMNoEndpoint,
    VLLMNotLoaded,
    get_vllm_base_url,
)

logger = logging.getLogger(__name__)

# --- thinking-mode model whitelist ---
# Models whose chat template honors `chat_template_kwargs.enable_thinking`.
# Match is by case-insensitive substring on the engine name. If a model is not
# listed, the `extra_body.thinking` field is silently ignored (per Step 2 spec
# decision C+A: whitelist with silent fallback).
_THINKING_MODEL_PATTERNS = (
    "qwen3",  # qwen3.5-35b, qwen3-8b, etc.
    "deepseek-r1",
    "deepseek-v3",
    "doubao-seed-1.8",
    "doubao-seed-2",
)


def supports_thinking(engine_name: str) -> bool:
    n = (engine_name or "").lower()
    return any(p in n for p in _THINKING_MODEL_PATTERNS)


def inject_thinking(body: dict, engine_name: str) -> None:
    """Translate `body['thinking'] = {'type': enabled|disabled|auto}` into
    `body['chat_template_kwargs']['enable_thinking'] = bool` for vLLM.

    - Pops `thinking` from body either way (vLLM rejects unknown top-level fields).
    - If model isn't whitelisted, silently drop (per Ark `extra_body` semantics:
      non-standard fields are best-effort, not hard contract).
    - `auto` = leave unset, let model default.
    """
    thinking = body.pop("thinking", None)
    if not isinstance(thinking, dict):
        return
    t = thinking.get("type")
    if t not in ("enabled", "disabled", "auto"):
        return
    if not supports_thinking(engine_name):
        return
    if t == "auto":
        return
    kwargs = body.setdefault("chat_template_kwargs", {})
    kwargs["enable_thinking"] = (t == "enabled")


async def preflight_quota(session: AsyncSession, api_key_id: int, service_id: int) -> None:
    """推理前拦已耗尽配额的 key(返回 402);无 grant 的 legacy key 放行。安全 P2。"""
    from src.services.quota_gate import preflight_check
    from src.services.resource_pack import QuotaExhausted
    try:
        await preflight_check(session, api_key_id=api_key_id, service_id=service_id)
    except QuotaExhausted as e:
        raise HTTPException(402, detail=f"quota exhausted: {e}")


async def post_consume_quota(api_key_id: int, service_id: int, units: int) -> None:
    """Charge `units` against the (api_key, service) grant post-inference.

    Best-effort: legacy keys (no grant) are silently skipped. allow_overshoot(H1):
    工作已交付,额度被并发抢光也强制记账(扣成负),不漏计 —— 旧代码在此吞
    QuotaExhausted → 输给 CAS 竞争的并发流式请求拿到免费未计费 token。preflight
    已把滥用收敛到 ~并发数,超扣由下个请求的 preflight 挡住自我修正。只有无 pack
    (无限量)grant 才会走到 QuotaExhausted 分支。
    """
    if units <= 0:
        return
    from src.models.database import get_session_factory
    from src.services.quota_gate import NoActiveGrant, consume_for_request
    from src.services.resource_pack import QuotaExhausted

    sf = get_session_factory()
    async with sf() as s:
        try:
            await consume_for_request(
                s, api_key_id=api_key_id, service_id=service_id, units=units,
                allow_overshoot=True,
            )
            await s.commit()
        except NoActiveGrant:
            return
        except QuotaExhausted:
            # 无 pack 的无限量 grant —— 无处可扣,正常跳过。
            logger.debug(
                "no resource pack for api_key=%s service=%s (unmetered)",
                api_key_id, service_id,
            )


async def granted_services(
    session: AsyncSession, api_key: InstanceApiKey,
) -> Sequence[ServiceInstance]:
    """该 key active-grant 的全部服务(ServiceInstance),按类目+名排序 —— 与
    /v1/chat·/v1/embeddings·/v1/images 同款 M:N scope。"""
    from sqlalchemy import select  # noqa: PLC0415

    from src.models.api_gateway import ApiKeyGrant  # noqa: PLC0415

    rows = await session.execute(
        select(ServiceInstance)
        .join(ApiKeyGrant, ApiKeyGrant.service_id == ServiceInstance.id)
        .where(
            ApiKeyGrant.api_key_id == api_key.id,
            ApiKeyGrant.status == "active",
        )
        .order_by(ServiceInstance.category, ServiceInstance.name)
    )
    return rows.scalars().all()


@dataclass(frozen=True)
class ChatEndpoint:
    engine_name: str
    base_url: str
    max_model_len: int


async def resolve_chat_endpoint(
    session: AsyncSession,
    *,
    model_mgr: Any,
    instance: ServiceInstance,
    api_key: InstanceApiKey | None,
    requested_model: str | None,
) -> ChatEndpoint:
    """model 服务 → 已加载 vLLM 的端点。未加载 → 503 model_not_ready(spec 2026-09-05 §5)。"""
    engine_name = instance.source_name or str(instance.source_id)
    try:
        base_url = get_vllm_base_url(model_mgr, engine_name)
    except VLLMNotLoaded as e:
        # 2026-09-05 spec §5:数据面对放置只读 —— 未就绪即刻 503,绝不在请求路径上加载。
        from src.api.routes._readiness import ready_model_names  # noqa: PLC0415
        raise ModelNotReadyError(
            requested_model or engine_name,
            ready_models=ready_model_names(
                model_mgr, await granted_services(session, api_key) if api_key else []),
        ) from e
    except VLLMNoEndpoint as e:
        raise HTTPException(500, detail=str(e)) from e
    # adapter 只用来读 max_model_len(下游 clamp max_tokens)。
    adapter = model_mgr.get_adapter(engine_name)
    max_model_len = getattr(adapter, "max_model_len", 4096) or 4096
    return ChatEndpoint(engine_name=engine_name, base_url=base_url, max_model_len=max_model_len)


def clamped_max_tokens(requested: int | None, max_model_len: int) -> int | None:
    """max_tokens 超过 max_model_len-512 时夹紧(给 prompt 留余量);没给 / 没超原样返回。"""
    if requested and requested > max_model_len - 512:
        return max(max_model_len - 512, max_model_len // 2)
    return requested


@dataclass(frozen=True)
class ChatHttpResult:
    status_code: int
    content: bytes
    data: dict[str, Any] | None  # 仅上游 200 时为解析后的 JSON


async def invoke_chat_nonstream(
    *,
    model_mgr: Any,
    endpoint: ChatEndpoint,
    body: dict[str, Any],
    instance: ServiceInstance,
    api_key: InstanceApiKey | None,
    agent_id: str | None = None,
) -> ChatHttpResult:
    """POST 到 vLLM,成功时记用量 + 扣配额(admin 会话不扣)。上游非 200 原样带回,不记账。"""
    # C3:请求期间对 engine 加引用,防 memory_guard / idle-TTL 中途 evict。finally 释放。
    proxy_ref = f"proxy-{uuid.uuid4().hex}"
    if model_mgr is not None:
        model_mgr.add_reference(endpoint.engine_name, proxy_ref)
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=300, proxy=None) as client:
            resp = await client.post(
                f"{endpoint.base_url.rstrip('/')}/v1/chat/completions", json=body)
        duration = int((time.monotonic() - start) * 1000)
        if resp.status_code != 200:
            return ChatHttpResult(status_code=resp.status_code, content=resp.content, data=None)
        data = resp.json()
        usage = data.get("usage") or {}
        # 函数内 import:tests 的 mock_vllm 按模块属性打桩 usage_service.record_llm_usage。
        from src.services.usage_service import record_llm_usage  # noqa: PLC0415
        await record_llm_usage(
            model=endpoint.engine_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            duration_ms=duration,
            instance_id=instance.id,
            api_key_id=api_key.id if api_key else None,
            agent_id=agent_id,
        )
        if api_key is not None:  # admin 会话(Playground)不扣配额
            await post_consume_quota(api_key.id, instance.id, usage.get("total_tokens", 0))
        return ChatHttpResult(status_code=200, content=resp.content, data=data)
    finally:
        # C3:非流式请求结束(含异常)释放 engine 引用。
        if model_mgr is not None:
            model_mgr.remove_reference(endpoint.engine_name, proxy_ref)
