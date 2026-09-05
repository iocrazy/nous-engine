"""Ollama-compatible endpoints: /api/chat, /api/generate, /api/tags, /api/show.

Protocol-layer shim. Auth, model resolution, and quota accounting are
the same as /v1/chat/completions — the only extra work is the JSON
reshaping handled by `src/services/ollama_adapter.py`.

Why Ollama support: the ecosystem has many tools (Continue.dev, Cursor,
LangChain integrations, etc.) that default to Ollama. Exposing a
bearer-token-protected Ollama surface lets them point at us without any
config gymnastics. The bearer token goes in `Authorization: Bearer ...`
just like our OpenAI surface.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_auth import verify_bearer_token_any
from src.api.routes.openai_compat import _post_consume_quota
from src.errors import (
    APIError,
    InvalidRequestError,
    ModelNotReadyError,
    NotFoundError,
    NousError,
)
from src.models.api_gateway import ApiKeyGrant
from src.models.database import get_async_session
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance
from src.services.inference.vllm_endpoint import (
    VLLMNoEndpoint,
    VLLMNotLoaded,
    get_vllm_base_url,
)
from src.services.model_resolver import ModelNotFound, resolve_target_service
from src.services.ollama_adapter import (
    ollama_chat_to_openai,
    ollama_generate_to_openai,
    openai_chat_to_ollama,
    openai_chat_to_ollama_generate,
    openai_sse_chunk_to_ollama_ndjson,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ollama-compat"])


async def _resolve_model_instance(
    session: AsyncSession,
    instance: ServiceInstance | None,
    api_key: InstanceApiKey,
    requested_model: str | None,
) -> ServiceInstance:
    """Shared resolver: legacy binding wins, else M:N via model name."""
    if instance is not None:
        # legacy 1:1 key —— 已在 verify_bearer_token_any 准入时占过坑。
        if instance.status != "active":
            raise HTTPException(403, detail="Instance is inactive")
        return instance
    try:
        resolved = await resolve_target_service(
            session, api_key=api_key, requested_model=requested_model,
        )
    except ModelNotFound as e:
        raise NotFoundError(str(e), code="model_not_found")
    # round9:补 status 检查 —— resolve_target_service 只过滤 grant.status==active、不查
    # ServiceInstance.status。openai_compat/anthropic_compat resolve 后都补了这个 403,
    # ollama M:N 分支早先漏了 → admin 停用(inactive)某 service 但 grant 仍 active 时,
    # M:N key 经 ollama 仍能命中出 token、经另两个表面则 403(授权/状态绕过不一致)。
    if resolved.status != "active":
        raise HTTPException(403, detail="Instance is inactive")
    # M:N key 在 auth 层没限流,解析出 instance 后补占坑(与 openai/anthropic 一致)。
    from src.api.deps_auth import enforce_instance_rate_limit
    await enforce_instance_rate_limit(resolved)
    return resolved


async def _get_adapter(request: Request, instance: ServiceInstance):
    """Resolve the loaded vLLM adapter or raise a clean 503."""
    if instance.source_type != "model":
        raise HTTPException(
            501,
            detail=f"{instance.source_type}-backed inference not yet supported "
                   f"on the Ollama surface",
        )
    engine_name = instance.source_name or str(instance.source_id)
    # spec §4.5 D6/D8: direct-to-vLLM HTTP. base-URL lookup via single source of truth.
    model_mgr = getattr(request.app.state, "model_manager", None)
    try:
        base_url = get_vllm_base_url(model_mgr, engine_name)
    except VLLMNotLoaded as e:
        # 2026-09-05 spec §5:数据面对放置只读 —— 未就绪即刻 503,绝不在请求路径上加载。
        raise ModelNotReadyError(engine_name) from e
    except VLLMNoEndpoint as e:
        raise HTTPException(500, detail=str(e)) from e
    # adapter handle still returned for downstream max_model_len clamp.
    adapter = model_mgr.get_adapter(engine_name)
    return adapter, engine_name, base_url


async def _stream_ollama_ndjson(
    base_url: str,
    openai_body: dict,
    public_model_name: str,
    api_key_id: int,
    instance_id: int,
    start_ms: float,
):
    """Proxy vLLM SSE, translate each chunk to Ollama NDJSON, post-consume."""
    openai_body.setdefault("stream_options", {})["include_usage"] = True
    usage_total = 0
    try:
        async with httpx.AsyncClient(timeout=300, proxy=None) as client:
            async with client.stream(
                "POST", f"{base_url.rstrip('/')}/v1/chat/completions",
                json=openai_body,
            ) as resp:
                if resp.status_code != 200:
                    err_text = (await resp.aread()).decode(errors="replace")
                    if resp.status_code == 404:
                        raise NotFoundError(err_text[:500], code="upstream_not_found")
                    if 400 <= resp.status_code < 500:
                        raise InvalidRequestError(err_text[:500], code="upstream_bad_request")
                    raise APIError("Upstream LLM error", code="upstream_error")
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    ndjson = openai_sse_chunk_to_ollama_ndjson(
                        line, model_name=public_model_name,
                    )
                    if ndjson is None:
                        continue
                    obj = json.loads(ndjson)
                    if obj.get("done") is True:
                        usage_total = (
                            obj.get("prompt_eval_count", 0)
                            + obj.get("eval_count", 0)
                        )
                    yield ndjson + "\n"
    except NousError as e:
        # Emit an Ollama-style error line then stop.
        yield json.dumps({
            "model": public_model_name,
            "error": e.to_dict(),
            "done": True,
        }) + "\n"
        return

    # Successful stream: record post-consume quota.
    await _post_consume_quota(api_key_id, instance_id, usage_total)


@router.post("/api/chat")
async def ollama_chat(
    request: Request,
    auth: tuple[ServiceInstance | None, InstanceApiKey] = Depends(
        verify_bearer_token_any,
    ),
    session: AsyncSession = Depends(get_async_session),
):
    instance_preauth, api_key = auth
    body = await request.json()
    requested_model = body.get("model") or None

    instance = await _resolve_model_instance(
        session, instance_preauth, api_key, requested_model,
    )
    adapter, engine_name, base_url = await _get_adapter(request, instance)

    openai_body = ollama_chat_to_openai(body)
    # vLLM rejects the client-facing model name; its internal path is "".
    openai_body["model"] = ""

    # Clamp max_tokens same as openai_compat does.
    max_model_len = getattr(adapter, "max_model_len", 4096) or 4096
    if openai_body.get("max_tokens") and openai_body["max_tokens"] > max_model_len - 512:
        openai_body["max_tokens"] = max(max_model_len - 512, max_model_len // 2)

    is_stream = bool(openai_body.get("stream"))
    start_ms = time.monotonic()

    if is_stream:
        return StreamingResponse(
            _stream_ollama_ndjson(
                base_url=base_url,
                openai_body=openai_body,
                public_model_name=requested_model or instance.name,
                api_key_id=api_key.id,
                instance_id=instance.id,
                start_ms=start_ms,
            ),
            media_type="application/x-ndjson",
        )

    async with httpx.AsyncClient(timeout=300, proxy=None) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/v1/chat/completions", json=openai_body,
        )
    if resp.status_code != 200:
        return Response(
            content=resp.content, status_code=resp.status_code,
            media_type="application/json",
        )
    data = resp.json()
    usage = data.get("usage", {})

    from src.services.usage_service import record_llm_usage
    duration = int((time.monotonic() - start_ms) * 1000)
    await record_llm_usage(
        model=engine_name,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        duration_ms=duration,
        instance_id=instance.id,
        api_key_id=api_key.id,
    )
    await _post_consume_quota(
        api_key.id, instance.id, usage.get("total_tokens", 0),
    )

    ollama_resp = openai_chat_to_ollama(
        data, model_name=requested_model or instance.name,
    )
    return Response(
        content=json.dumps(ollama_resp), media_type="application/json",
    )


@router.post("/api/generate")
async def ollama_generate(
    request: Request,
    auth: tuple[ServiceInstance | None, InstanceApiKey] = Depends(
        verify_bearer_token_any,
    ),
    session: AsyncSession = Depends(get_async_session),
):
    instance_preauth, api_key = auth
    body = await request.json()
    requested_model = body.get("model") or None

    instance = await _resolve_model_instance(
        session, instance_preauth, api_key, requested_model,
    )
    adapter, engine_name, base_url = await _get_adapter(request, instance)

    openai_body = ollama_generate_to_openai(body)
    openai_body["model"] = ""

    max_model_len = getattr(adapter, "max_model_len", 4096) or 4096
    if openai_body.get("max_tokens") and openai_body["max_tokens"] > max_model_len - 512:
        openai_body["max_tokens"] = max(max_model_len - 512, max_model_len // 2)

    is_stream = bool(openai_body.get("stream"))
    start_ms = time.monotonic()

    if is_stream:
        # /api/generate streams too but uses "response" key, not "message".
        # Reuse the chat streamer then remap the key on each line.
        async def _remap_chat_to_generate():
            async for line in _stream_ollama_ndjson(
                base_url=base_url,
                openai_body=openai_body,
                public_model_name=requested_model or instance.name,
                api_key_id=api_key.id,
                instance_id=instance.id,
                start_ms=start_ms,
            ):
                try:
                    obj = json.loads(line)
                except Exception:
                    yield line
                    continue
                msg = obj.pop("message", None)
                if msg is not None:
                    obj["response"] = msg.get("content", "")
                yield json.dumps(obj) + "\n"

        return StreamingResponse(
            _remap_chat_to_generate(), media_type="application/x-ndjson",
        )

    async with httpx.AsyncClient(timeout=300, proxy=None) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/v1/chat/completions", json=openai_body,
        )
    if resp.status_code != 200:
        return Response(
            content=resp.content, status_code=resp.status_code,
            media_type="application/json",
        )
    data = resp.json()
    usage = data.get("usage", {})

    from src.services.usage_service import record_llm_usage
    duration = int((time.monotonic() - start_ms) * 1000)
    await record_llm_usage(
        model=engine_name,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        duration_ms=duration,
        instance_id=instance.id,
        api_key_id=api_key.id,
    )
    await _post_consume_quota(
        api_key.id, instance.id, usage.get("total_tokens", 0),
    )

    ollama_resp = openai_chat_to_ollama_generate(
        data, model_name=requested_model or instance.name,
    )
    return Response(
        content=json.dumps(ollama_resp), media_type="application/json",
    )


# ---------- /api/tags and /api/show ----------


@router.get("/api/tags")
async def ollama_tags(
    request: Request,
    auth: tuple[ServiceInstance | None, InstanceApiKey] = Depends(
        verify_bearer_token_any,
    ),
    session: AsyncSession = Depends(get_async_session),
):
    """List models this key has active access to.

    Legacy keys: just the one bound instance. M:N keys: all instances
    reached via active ApiKeyGrant rows. Empty list is valid — no 401.

    2026-09-05 spec §6:model 类服务只在其模型**已加载**时出现,口径与 `/v1/models`
    完全一致(共用 `_readiness.service_is_ready`)。此前 Ollama 面把冷模型也列出来,
    客户端(Open WebUI 等)选中即 503 —— 发现到的必须等于现在就能调的。
    comfy_template / workflow / app 类照旧按授权列。
    """
    instance, api_key = auth

    if instance is not None:
        rows = [instance]
    else:
        stmt = (
            select(ServiceInstance)
            .join(ApiKeyGrant, ApiKeyGrant.service_id == ServiceInstance.id)
            .where(
                ApiKeyGrant.api_key_id == api_key.id,
                ApiKeyGrant.status == "active",
                ServiceInstance.status == "active",
            )
        )
        rows = (await session.execute(stmt)).scalars().all()

    from src.api.routes._readiness import service_is_ready  # noqa: PLC0415
    model_mgr = getattr(request.app.state, "model_manager", None)

    models = []
    for inst in rows:
        if not service_is_ready(model_mgr, inst):
            continue
        models.append({
            "name": inst.name,
            "model": inst.name,
            "modified_at": "1970-01-01T00:00:00Z",
            "size": 0,
            "digest": f"sha256:{inst.id:064x}"[:71],
            "details": {
                "format": "gguf",
                "family": inst.category or (inst.type or "llm"),
                "families": [inst.category or (inst.type or "llm")],
                "parameter_size": "unknown",
                "quantization_level": "unknown",
            },
        })
    return {"models": models}


@router.post("/api/show")
async def ollama_show(
    body: dict,
    auth: tuple[ServiceInstance | None, InstanceApiKey] = Depends(
        verify_bearer_token_any,
    ),
    session: AsyncSession = Depends(get_async_session),
):
    """Return metadata for a single model name.

    Ollama's response carries `license`, `modelfile`, `parameters`,
    `template`, `details`. We return stubs for the textual fields and
    real `details` where we can.
    """
    instance_preauth, api_key = auth
    requested = body.get("name") or body.get("model")
    if not requested:
        raise HTTPException(400, detail="Missing 'name' field")

    instance = await _resolve_model_instance(
        session, instance_preauth, api_key, requested,
    )

    return {
        "license": "",
        "modelfile": "",
        "parameters": "",
        "template": "",
        "details": {
            "format": "gguf",
            "family": instance.category or (instance.type or "llm"),
            "families": [instance.category or (instance.type or "llm")],
            "parameter_size": "unknown",
            "quantization_level": "unknown",
        },
    }
