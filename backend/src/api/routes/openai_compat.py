"""OpenAI-compatible endpoints: chat/completions, audio/speech, models."""

import asyncio
import io
import json
import logging
import os
import re
import tempfile
import time
import uuid
import wave
from typing import Any, Literal

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_auth import verify_bearer_token_any
from src.config import get_settings
from src.errors import (
    APIError,
    InvalidRequestError,
    ModelNotReadyError,
    NotFoundError,
    NousError,
)
from src.models.database import get_async_session
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance
from src.services.inference.vllm_endpoint import (
    VLLMNoEndpoint,
    VLLMNotLoaded,
    get_vllm_base_url,
)
from src.services.model_resolver import ModelNotFound, resolve_target_service
from src.services.prompt_composer import (
    AgentLoadFailed,
    AgentNotFound,
)
from src.services.prompt_composer import (
    compose as compose_agent_prompt,
)
from src.services.skill_tools import skill_tool_schema

logger = logging.getLogger(__name__)

router = APIRouter(tags=["openai-compat"])

# round3 #1:持有 fire-and-forget 结算 task 的强引用直到完成。CPython 事件循环只持 task
# 弱引用,未被引用的 task 可能在跑完前被 GC → 结算(记账+扣配额)协程中途消失。
# done_callback 在完成时移除引用,避免 set 无界增长。
_settle_tasks: set[asyncio.Task] = set()


async def sse_with_error_envelope(inner):
    """Wrap an SSE async generator so any NousError/Exception is emitted as an
    OpenAI-style error chunk followed by exactly one `data: [DONE]`.

    - Strips stray `data: [DONE]` markers emitted by the inner generator so we
      always emit exactly one terminator from the wrapper.
    - Converts NousError via to_dict(); any other Exception becomes a generic
      APIError (no traceback leak).
    """
    try:
        async for chunk in inner:
            if chunk.strip() == "data: [DONE]":
                # wrapper owns the terminator
                continue
            yield chunk
    except NousError as e:
        yield f"data: {json.dumps(e.to_dict())}\n\n"
    except Exception:
        logger.exception("SSE stream failure")
        err = APIError("Internal server error", code="internal_error")
        yield f"data: {json.dumps(err.to_dict())}\n\n"
    finally:
        yield "data: [DONE]\n\n"


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


def _supports_thinking(engine_name: str) -> bool:
    n = (engine_name or "").lower()
    return any(p in n for p in _THINKING_MODEL_PATTERNS)


async def _preflight_quota(session, api_key_id: int, service_id: int) -> None:
    """推理前拦已耗尽配额的 key(返回 402);无 grant 的 legacy key 放行。安全 P2。"""
    from src.services.quota_gate import preflight_check
    from src.services.resource_pack import QuotaExhausted
    try:
        await preflight_check(session, api_key_id=api_key_id, service_id=service_id)
    except QuotaExhausted as e:
        raise HTTPException(402, detail=f"quota exhausted: {e}")


async def _post_consume_quota(api_key_id: int, service_id: int, units: int) -> None:
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


def _maybe_inject_thinking(body: dict, engine_name: str) -> None:
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
    if not _supports_thinking(engine_name):
        return
    if t == "auto":
        return
    kwargs = body.setdefault("chat_template_kwargs", {})
    kwargs["enable_thinking"] = (t == "enabled")


async def _auth_bearer_or_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> tuple[ServiceInstance | None, InstanceApiKey | None]:
    """数据面 auth:Bearer 优先(走 key+grant+quota,外部调用);否则 admin session
    旁路(给 in-app Playground 用 —— 同 /v1/apps 的 _auth_apps_run,否则 cookie 调
    会被 verify_bearer_token_any 的必填 Authorization 卡 400)。(None,None)=admin。

    用于 transcriptions / chat.completions / embeddings。后两者 2026-09-22 前还挂在
    verify_bearer_token_any 上:Playground 点运行直接 400「Field required」
    (param=header.authorization),业务代码一行没跑到。"""
    if authorization:
        return await verify_bearer_token_any(authorization, session)
    from src.api.admin_session import request_is_authed
    if request_is_authed(request):
        return None, None
    raise HTTPException(401, detail="Missing API key or admin session")


async def _admin_lookup_service(session: AsyncSession, name: str | None) -> ServiceInstance:
    """admin 会话(Playground)按服务名直查 —— 单管理员隐式授权,跳 grant/quota。"""
    from sqlalchemy import select  # noqa: PLC0415
    instance = (
        await session.execute(select(ServiceInstance).where(ServiceInstance.name == name))
    ).scalar_one_or_none()
    if instance is None:
        raise NotFoundError(f"service '{name}' not found", code="service_not_found")
    return instance


# --- /v1/chat/completions ---

@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    auth: tuple[ServiceInstance | None, InstanceApiKey | None] = Depends(_auth_bearer_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """OpenAI-compatible chat completions with token metering.

    Dispatch:
      - Legacy keys (instance_id bound) → use that instance, ignore body.model
      - M:N keys → look up active ApiKeyGrant by body.model (ServiceInstance.name)

    After resolution, instance.source_type selects the handler:
      - "model"    → vllm subprocess (below)
      - "workflow" → WorkflowExecutor (not yet wired; 501)
      - "app"      → 501 (v3: app == workflow-backed service; routed via /v1/apps)
    """
    instance, api_key = auth

    body = await request.json()
    # SSRF 防护:messages[].content[].image_url 会被 vLLM 服务端 fetch,校验拒私网/http
    # (与 understand 口径一致;放行 data:/公网 https)。
    from src.utils.url_security import UnsafeURLError, validate_chat_image_urls
    try:
        await validate_chat_image_urls(body.get("messages"))
    except UnsafeURLError as e:
        raise InvalidRequestError(str(e), code="unsafe_image_url")
    requested_model = body.get("model") or None
    admin_run = api_key is None  # admin session 旁路(Playground):跳 grant/quota/计费归属

    # Resolve target service. Legacy 1:1 keys (instance set by the auth dep)
    # short-circuit; M:N keys use the v3 grant lookup.
    if admin_run:
        instance = await _admin_lookup_service(session, requested_model)
    elif instance is None:
        try:
            instance = await resolve_target_service(
                session, api_key=api_key, requested_model=requested_model,
            )
        except ModelNotFound as e:
            raise NotFoundError(str(e), code="model_not_found")
        if instance.status != "active":
            raise HTTPException(403, detail="Instance is inactive")
        # M:N key 在 auth 层(verify_bearer_token_any 返回 None instance)没限流,
        # 解析出目标 instance 后必须补占坑,否则 M:N key 完全绕过 RPM/TPM。
        from src.api.deps_auth import enforce_instance_rate_limit
        await enforce_instance_rate_limit(instance)
        await _preflight_quota(session, api_key.id, instance.id)
        # v3: dispatch needs the snapshot if the service is workflow-backed.
        # Force-load deferred columns now so handlers below see real data
        # (covered by test_services_dispatch.py SQL counter assertion).
        await session.refresh(
            instance,
            attribute_names=["workflow_snapshot", "exposed_inputs", "exposed_outputs"],
        )

    # Dispatch by source_type
    if instance.source_type == "workflow":
        raise HTTPException(
            501,
            detail="workflow-backed chat/completions not yet implemented",
        )
    if instance.source_type == "app":
        raise HTTPException(
            501,
            detail="app-backed chat/completions not yet implemented",
        )
    if instance.source_type != "model":
        raise HTTPException(
            400,
            detail=f"Unsupported instance source_type: {instance.source_type}",
        )

    engine_name = instance.source_name or str(instance.source_id)
    # spec §4.5 D6/D8: direct-to-vLLM HTTP. base-URL lookup via single source of truth.
    model_mgr = getattr(request.app.state, "model_manager", None)
    try:
        base_url = get_vllm_base_url(model_mgr, engine_name)
    except VLLMNotLoaded as e:
        # 2026-09-05 spec §5:数据面对放置只读 —— 未就绪即刻 503,绝不在请求路径上加载。
        from src.api.routes._readiness import ready_model_names  # noqa: PLC0415
        raise ModelNotReadyError(
            body.get("model") or engine_name,
            ready_models=ready_model_names(
                model_mgr, await _granted_services(session, api_key) if api_key else []),
        ) from e
    except VLLMNoEndpoint as e:
        raise HTTPException(500, detail=str(e)) from e

    # Adapter handle still needed downstream for max_model_len clamp (line ~283).
    adapter = model_mgr.get_adapter(engine_name)

    body["model"] = ""  # vLLM uses its own model path

    # Resolve agent (top-level or extra_body.agent). vLLM rejects unknown
    # top-level fields, so always pop — even when injection is disabled.
    agent_id = body.pop("agent", None)
    if not agent_id and isinstance(body.get("extra_body"), dict):
        agent_id = body["extra_body"].pop("agent", None)
        if not body["extra_body"]:
            body.pop("extra_body", None)

    # Compose agent system message (chat/completions has no session concept,
    # so there's no binding check — every request is independent).
    settings = get_settings()
    agent_sys: str | None = None
    if settings.NOUS_ENABLE_AGENT_INJECTION and agent_id:
        try:
            agent_sys = compose_agent_prompt(agent_id, None)
        except AgentNotFound:
            raise InvalidRequestError(
                f"agent not found: {agent_id}",
                code="agent_not_found",
            )
        except AgentLoadFailed as e:
            logger.error("agent load failed: %s", e)
            raise APIError(
                f"failed to load agent {agent_id}",
                code="agent_load_failed",
            )

    if agent_sys is not None:
        messages = list(body.get("messages") or [])
        messages.insert(0, {"role": "system", "content": agent_sys})
        body["messages"] = messages
        # Inject Skill tool schema when an agent is active.
        tools_list = list(body.get("tools") or [])
        tools_list.insert(0, skill_tool_schema())
        body["tools"] = tools_list

    # Resolve context_id (top-level or extra_body.context_id)
    context_id = body.pop("context_id", None)
    if not context_id and isinstance(body.get("extra_body"), dict):
        context_id = body["extra_body"].pop("context_id", None)
        if not body["extra_body"]:
            body.pop("extra_body", None)

    if context_id and admin_run:
        # 上下文缓存按 key 归属(owner_key_id);admin 会话(Playground)没有 key。
        raise InvalidRequestError(
            "context_id 需要 API key(上下文缓存按 key 归属),Playground 的 admin 会话不支持",
            code="context_requires_api_key",
        )
    if context_id:
        from src.models.database import get_session_factory as _csf
        from src.services.context_cache_service import (
            increment_hit_and_extend as _ihe,
        )
        from src.services.context_cache_service import (
            resolve_for_request,
        )

        sf = _csf()
        async with sf() as cache_session:
            cached_messages, cached_ttl = await resolve_for_request(
                cache_session,
                context_id=context_id,
                owner_key_id=api_key.id,
                engine_name=engine_name,
            )
        if cached_messages:
            body["messages"] = cached_messages + list(body.get("messages", []))

        # Fire-and-forget hit-count update; loop persists across requests under uvicorn.
        async def _bump(cid: str = context_id, ttl: int = cached_ttl, kid: int = api_key.id):
            try:
                async with _csf()() as s2:
                    await _ihe(s2, cid, ttl, owner_key_id=kid)
            except Exception:
                logger.exception("hit_count update failed for %s", cid)
        # 持强引用直到完成,否则 fire-and-forget task 可能被 GC 中途回收(同 _settle,round3 #1)。
        _bt = asyncio.create_task(_bump())
        _settle_tasks.add(_bt)
        _bt.add_done_callback(_settle_tasks.discard)

    # OpenAI SDK extra_body.thinking → vLLM chat_template_kwargs.enable_thinking
    # Whitelist-driven; silent ignore for unsupported models (Step 2 spec).
    _maybe_inject_thinking(body, engine_name)

    # Clamp max_tokens
    max_model_len = getattr(adapter, "max_model_len", 4096) or 4096
    if body.get("max_tokens") and body["max_tokens"] > max_model_len - 512:
        body["max_tokens"] = max(max_model_len - 512, max_model_len // 2)

    is_stream = body.get("stream", False)
    start_ms = time.monotonic()

    # C3:代理请求期间对 engine 加引用,防 memory_guard(每 5s 查 free<4GB,而 vLLM 常态
    # 吃到 ~90%)或 idle-TTL 在**流式输出中途**把这个正在服务的 vLLM 进程 evict 掉 →
    # 客户端连接被硬断。ref 在下面 streaming/non-streaming 两条路径的 finally 里释放。
    # 放在选路后、真正发请求前 —— 之前的 inject/clamp 是纯 dict 操作,不会 raise 泄漏。
    proxy_ref = f"proxy-{uuid.uuid4().hex}"
    if model_mgr is not None:
        model_mgr.add_reference(engine_name, proxy_ref)

    if is_stream:
        # Streaming: inject include_usage, proxy SSE chunks
        body.setdefault("stream_options", {})["include_usage"] = True

        async def _stream_proxy():
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            try:
                async with httpx.AsyncClient(timeout=300, proxy=None) as client:
                    async with client.stream(
                        "POST", f"{base_url.rstrip('/')}/v1/chat/completions", json=body
                    ) as resp:
                        if resp.status_code != 200:
                            error_text = (await resp.aread()).decode(errors="replace")
                            # Map upstream status to a NousError so the wrapper
                            # formats it uniformly.
                            if resp.status_code == 404:
                                raise NotFoundError(error_text[:500], code="upstream_not_found")
                            if 400 <= resp.status_code < 500:
                                raise InvalidRequestError(error_text[:500], code="upstream_bad_request")
                            raise APIError("Upstream LLM error", code="upstream_error")
                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            yield line + "\n"
                            # Extract usage from final chunk
                            if line.startswith("data: ") and line[6:] != "[DONE]":
                                try:
                                    chunk = json.loads(line[6:])
                                    if "usage" in chunk and chunk["usage"]:
                                        usage = chunk["usage"]
                                except Exception:
                                    pass
                        yield "\n"
            finally:
                # C3:流结束/中途断连都释放 engine 引用,让它重新可被 evict。
                if model_mgr is not None:
                    model_mgr.remove_reference(engine_name, proxy_ref)
                # round2 #7:记账/扣配额放 finally —— 否则客户端中途断连(generator 被取消)
                # 时,流后的结算代码不执行 = 已生成的 token 不记账、不扣配额(漏收入)。用
                # create_task 把结算跟 generator 取消解耦(record/consume 各自开 session);只在
                # 拿到 usage(含 token 数的末帧已到)时结算 —— 纯错误/早断连无计数,跳过免噪音。
                tok = usage.get("total_tokens", 0) or usage.get("completion_tokens", 0)
                if tok > 0:
                    duration = int((time.monotonic() - start_ms) * 1000)
                    _u = dict(usage)

                    async def _settle() -> None:
                        try:
                            from src.services.usage_service import record_llm_usage
                            await record_llm_usage(
                                model=engine_name,
                                prompt_tokens=_u.get("prompt_tokens", 0),
                                completion_tokens=_u.get("completion_tokens", 0),
                                duration_ms=duration,
                                instance_id=instance.id,
                                api_key_id=api_key.id if api_key else None,
                                agent_id=agent_id if settings.NOUS_ENABLE_AGENT_INJECTION else None,
                            )
                            if api_key is not None:  # admin 会话(Playground)不扣配额
                                await _post_consume_quota(
                                    api_key.id, instance.id, _u.get("total_tokens", 0),
                                )
                        except Exception as e:  # noqa: BLE001 — 结算失败不该崩流
                            logger.warning("stream billing settle failed: %s", e)

                    # 持强引用直到完成,否则正常跑完的流式也可能被 GC 掉结算 task(round3 #1)。
                    _t = asyncio.create_task(_settle())
                    _settle_tasks.add(_t)
                    _t.add_done_callback(_settle_tasks.discard)

        return StreamingResponse(
            sse_with_error_envelope(_stream_proxy()),
            media_type="text/event-stream",
        )

    else:
        # Non-streaming: proxy request, extract usage
        try:
            async with httpx.AsyncClient(timeout=300, proxy=None) as client:
                resp = await client.post(f"{base_url.rstrip('/')}/v1/chat/completions", json=body)

            duration = int((time.monotonic() - start_ms) * 1000)

            if resp.status_code != 200:
                return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")

            data = resp.json()
            usage = data.get("usage", {})

            # Record usage
            from src.services.usage_service import record_llm_usage
            await record_llm_usage(
                model=engine_name,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                duration_ms=duration,
                instance_id=instance.id,
                api_key_id=api_key.id if api_key else None,
                agent_id=agent_id if settings.NOUS_ENABLE_AGENT_INJECTION else None,
            )
            if api_key is not None:  # admin 会话(Playground)不扣配额
                await _post_consume_quota(
                    api_key.id, instance.id, usage.get("total_tokens", 0),
                )

            return Response(content=resp.content, media_type="application/json")
        finally:
            # C3:非流式请求结束(含异常)释放 engine 引用。
            if model_mgr is not None:
                model_mgr.remove_reference(engine_name, proxy_ref)


# --- /v1/audio/speech ---

class SpeechRequest(BaseModel):
    model: str = "cosyvoice2"
    input: str
    voice: str = "default"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    response_format: Literal["wav", "mp3", "opus", "flac"] = "wav"


CONTENT_TYPE_MAP = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "opus": "audio/opus",
    "flac": "audio/flac",
}


@router.post("/v1/audio/speech")
async def create_speech(
    req: SpeechRequest,
    # PR-5a:legacy verify_bearer_token(只认 1:1 key,M:N 实际 403)→ verify_bearer_token_any。
    # handler 不用 instance(只按 req.model 取 engine),M:N 有效 key 即可。
    auth: tuple[ServiceInstance | None, InstanceApiKey] = Depends(verify_bearer_token_any),
):
    """Generate audio from text (OpenAI TTS compatible)."""
    from src.workers.tts_engines import registry

    engine = registry._ENGINE_INSTANCES.get(req.model)
    if engine is None or not engine.is_loaded:
        raise HTTPException(
            409,
            detail=f"Model '{req.model}' is not loaded. Load it first via POST /api/v1/engines/{req.model}/load",
        )

    try:
        # to_thread:同步阻塞 CUDA 调用,直接 await 卡死事件循环(round2 低)。
        result = await asyncio.to_thread(
            engine.synthesize,
            text=req.input,
            voice=req.voice,
            speed=req.speed,
        )
    except Exception as e:
        raise HTTPException(500, detail=str(e))

    audio_bytes = result.audio_bytes

    # Format conversion if needed (engine returns wav by default)
    if req.response_format != "wav" and req.response_format != result.format:
        try:
            audio_bytes = _convert_audio(audio_bytes, result.format, req.response_format, result.sample_rate)
        except Exception:
            # If conversion fails, return wav
            pass

    content_type = CONTENT_TYPE_MAP.get(req.response_format, "audio/wav")
    return Response(content=audio_bytes, media_type=content_type)


def _convert_audio(audio_bytes: bytes, src_fmt: str, dst_fmt: str, sample_rate: int) -> bytes:
    """Convert audio format using soundfile."""
    import soundfile as sf

    buf_in = io.BytesIO(audio_bytes)
    data, sr = sf.read(buf_in, dtype="float32")

    buf_out = io.BytesIO()
    fmt_map = {"wav": "WAV", "flac": "FLAC", "opus": "OGG"}
    sf_fmt = fmt_map.get(dst_fmt)
    if sf_fmt is None:
        raise ValueError(f"Unsupported output format: {dst_fmt}")

    sf.write(buf_out, data, sr, format=sf_fmt)
    buf_out.seek(0)
    return buf_out.read()


# --- /v1/embeddings ---


@router.post("/v1/embeddings")
async def embeddings(
    request: Request,
    auth: tuple[ServiceInstance | None, InstanceApiKey | None] = Depends(_auth_bearer_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """OpenAI 兼容 embeddings(2026-06-12 embedding 模态接入)。

    解析链与 chat/completions 同款:body.model → 服务(M:N grant / legacy 1:1)→
    source_type=model → vLLM 子进程(models.yaml `vllm_runner: pooling` 起的
    pooling 实例,如 qwen3_embedding_4b/8b)→ 透传 `/v1/embeddings`。
    body.model 置空让 vLLM 用自己的 served 模型(同 chat 的处理);input/
    encoding_format/dimensions 等字段原样透传。usage 计 prompt_tokens
    (embedding 无 completion)。
    """
    instance, api_key = auth

    body = await request.json()
    requested_model = body.get("model") or None

    if api_key is None:  # admin session 旁路(Playground):按服务名直查,跳 grant/quota
        instance = await _admin_lookup_service(session, requested_model)
    elif instance is None:
        try:
            instance = await resolve_target_service(
                session, api_key=api_key, requested_model=requested_model,
            )
        except ModelNotFound as e:
            raise NotFoundError(str(e), code="model_not_found")
        if instance.status != "active":
            raise HTTPException(403, detail="Instance is inactive")
        from src.api.deps_auth import enforce_instance_rate_limit
        await enforce_instance_rate_limit(instance)
        await _preflight_quota(session, api_key.id, instance.id)

    if instance.source_type != "model":
        raise HTTPException(
            400,
            detail=f"embeddings 只支持 model-backed 服务(got source_type={instance.source_type})",
        )

    engine_name = instance.source_name or str(instance.source_id)
    model_mgr = getattr(request.app.state, "model_manager", None)
    try:
        base_url = get_vllm_base_url(model_mgr, engine_name)
    except VLLMNotLoaded as e:
        # 2026-09-05 spec §5:同 chat —— 只读解析,未就绪即刻 503。
        from src.api.routes._readiness import ready_model_names  # noqa: PLC0415
        raise ModelNotReadyError(
            body.get("model") or engine_name,
            ready_models=ready_model_names(
                model_mgr, await _granted_services(session, api_key) if api_key else []),
        ) from e
    except VLLMNoEndpoint as e:
        raise HTTPException(500, detail=str(e)) from e

    body["model"] = ""  # vLLM uses its own model path(同 chat)

    start_ms = time.monotonic()
    async with httpx.AsyncClient(timeout=120, proxy=None) as client:
        resp = await client.post(f"{base_url.rstrip('/')}/v1/embeddings", json=body)

    duration = int((time.monotonic() - start_ms) * 1000)
    if resp.status_code != 200:
        return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")

    data = resp.json()
    usage = data.get("usage", {}) or {}
    from src.services.usage_service import record_llm_usage
    await record_llm_usage(
        model=engine_name,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=0,
        duration_ms=duration,
        instance_id=instance.id,
        api_key_id=api_key.id if api_key else None,
        agent_id=None,
    )
    if api_key is not None:  # admin 会话(Playground)不扣配额
        await _post_consume_quota(
            api_key.id, instance.id, usage.get("total_tokens", 0),
        )
    # 响应里的 model 回填服务名(对外契约:caller 看到自己请求的 model 名,不暴露本地路径)
    data["model"] = requested_model or engine_name
    return data


# --- /v1/audio/transcriptions ---


async def _ffmpeg_to_wav16k(raw: bytes) -> bytes:
    """任意音频 → 16kHz/单声道/PCM-s16le WAV。

    vLLM 的 ASR 端点先用 soundfile、回退 PyAV 解码上传音频;非常规格式(IEEE-Float
    WAV 等)易被拒。统一 ffmpeg 归一化成标准 PCM 再转发,兼容各种上传格式且确保稳解码。

    **输入/输出都用可 seek 的临时文件**(2026-06-21 真机踩,两类坑):
    - 输入若从不可 seek 的 pipe:0 读,moov-atom 在末尾的 m4a/mp4 等需 seek 的容器
      读不到音轨 → 输出空音频 → vLLM 处理器报 `audio=[array([], dtype=float32)]`。
    - 输出若写不可 seek 的 pipe:1,WAV 头的 RIFF/data size 回填不了(写 0xFFFFFFFF 占位)。
    临时文件可 seek,两者都解决。

    空音频(无有效音轨/无声/损坏)→ 直接报清晰 400,**不**把空数组转发给 vLLM
    (否则用户只看到晦涩的 Qwen3ASRProcessor empty-array 报错)。
    """
    with tempfile.NamedTemporaryFile(suffix=".in", delete=False) as in_tf:
        in_tf.write(raw)
        in_path = in_tf.name
    out_path = in_path + ".wav"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", in_path, "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", out_path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            msg = err.decode("utf-8", "ignore")[:200] if err else "unknown"
            raise InvalidRequestError(f"音频解码失败(ffmpeg): {msg}")
        with open(out_path, "rb") as f:
            out = f.read()
        # WAV header(含 ffmpeg 的 LIST/INFO chunk)~80-120 字节;有任何可用音频都是 KB 级。
        # < 1KB 视为空(header-only)→ ffmpeg 读到了文件但解出 0 采样。
        if len(out) < 1024:
            raise InvalidRequestError("音频无有效音轨或为空(可能不是有效音频文件、无声、或格式损坏)")
        return out
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass




def _wav16k_seconds(wav: bytes) -> int:
    """16k/mono/s16le WAV 的时长秒(metering 用;chat 路径不回 audio 秒数,自算)。"""
    try:
        with wave.open(io.BytesIO(wav), "rb") as w:
            fr = w.getframerate() or 16000
            return max(1, round(w.getnframes() / fr))
    except Exception:
        # 退化:s16le mono 16k → 每秒 32000 字节(减去 ~100 字节头开销可忽略)
        return max(1, round(len(wav) / 32000))


# MOSS verbose_json 的 segments[].text 形如 `[S01]正文`(说话人无独立字段,是文本前缀,
# 真机实测见 infra/moss-asr/SPIKE.md)。抠出 [Sxx] 前缀作 speaker,剩余为纯文本。
_MOSS_SPEAKER_RE = re.compile(r"^\s*\[(S\d+)\]\s*(.*)$", re.DOTALL)

# MOSS 的默认「转写 + 说话人分离」指令,逐字取自 sglang-omni 服务端
# `sglang_omni/models/moss_transcribe_diarize/request_builders.py:41-45`
# 的 DEFAULT_TRANSCRIBE_DIARIZE_PROMPT(亦即 MOSS 模型卡 README 公开的推荐 prompt,
# 属稳定契约)。context 热词经服务端 `prompt` 入参下传,但该入参**整体替换**默认指令
# (`_prompt_from_payload`:170-198,非空 prompt 直接作 user text、:182-183 仅空时才用
# 默认)——故我们自带这份默认指令、在尾部拼热词,既保 [Sxx]/时间戳输出契约,又生效热词。
# 即便服务端某次升级改了内部默认,我们这份仍是模型卡官方 prompt,输出契约不破。
# 基底 = 模型卡官方 prompt,**故意增补一句标点条款**(2026-07-22 真机实验):快速连续语流
# (无停顿口播)下 MOSS 用官方默认 prompt 输出 0 标点(1.8x 变速播客复现:2796 字 0 标点);
# 把标点要求写进主指令 → 220 标点/密度 7.3%,正常语速与 golden 均零回归(54 段/双说话人/
# 时间戳完好)。热词后缀 steer 无效(试过),必须在主指令内。
_MOSS_DEFAULT_PROMPT = (
    "请将音频转写为文本，并为正文添加规范的标点符号（逗号、句号、问号等），"
    "每一段需以起始时间戳和说话人编号"
    "（[S01]、[S02]、[S03]…）开头，正文为对应的语音内容，"
    "并在段末标注结束时间戳，以清晰标明该段语音范围。"
)


def _resolve_moss_base_url(model_mgr, engine_name: str) -> str:
    """MOSS 引擎 base_url 选址(spec 2026-07-20 Arc 2)。优先级 **env > ModelManager**:

    - `NOUS_MOSS_ASR_URL` 显式设了 → 用它(测试/应急 override,如指向手工起的实例)。
    - 否则 → 经 ModelManager 取 `moss_transcribe_diarize` resident 引擎的 base_url
      (只读:MOSS 是 resident,未加载即 VLLMNotLoaded(调用方映射 503),不在请求路径上拉起;
      SGLangOmniAdapter 暴露 `base_url`/`is_loaded`,与该 helper 契约一致直接复用)。

    未加载 → VLLMNotLoaded(调用方映射 503);已加载但无端点 → VLLMNoEndpoint(500)。
    """
    override = os.environ.get("NOUS_MOSS_ASR_URL")
    if override:
        return override.rstrip("/")
    return get_vllm_base_url(model_mgr, engine_name).rstrip("/")


async def _asr_moss_transcribe(
    client: httpx.AsyncClient, wav: bytes, context: str | None, audio_seconds: int,
    base_url: str,
) -> tuple[str, str | None, list[dict]]:
    """走 MOSS-Transcribe-Diarize SGLang 微服务(spec 2026-07-20)做转写 + 说话人分离。

    POST 归一化 16k WAV 到 `{base_url}/v1/audio/transcriptions`(base_url 由
    `_resolve_moss_base_url` 选址:env override > ModelManager),response_format=
    verbose_json 拿到段级(时间戳 + [Sxx] 说话人)结果。返回 `(text, language, segments)`:
    - text = 各段纯文本顺序拼接(剥掉时间戳/[Sxx],纯文本客户端不退化);
    - language = MOSS 响应有 language 字段就透传,没有则 None(防御式,不假设有);
    - segments = [{start, end, speaker, text}](start/end 保留秒 float,speaker 抠自 [Sxx])。

    MOSS 是唯一 ASR 主路(退役了 Qwen3-ASR + aligner),不可达/非 200 → HTTPException 503,
    **不静默降级**(spec §3)。

    context(领域/热词偏置)非空时:下传 `prompt = 默认指令 + 热词提示:{context}`(热词后缀
    = MOSS 模型卡 README 官方配方,transformers 真机 smoke 验过 steer 生效且不幻觉;见
    `_MOSS_DEFAULT_PROMPT` 处注释)。context 为空:不传 `prompt`,吃服务端默认指令。
    """
    base = base_url
    # max_new_tokens 按时长动态:0.9B 输出 ≈12.5 audio-token/s + 文本,盈余给标点/说话人标记;
    # 下限 5120 保短音频,上限 65536 防长音频失控。
    max_new_tokens = min(65536, max(5120, int(audio_seconds * 32) + 512))
    # 超时按时长给:长音频转写慢,至少 300s。
    timeout = max(300, audio_seconds * 2)
    files = {"file": ("audio.wav", wav, "audio/wav")}
    data = {
        "model": "moss-transcribe-diarize",
        "response_format": "verbose_json",
        "max_new_tokens": str(max_new_tokens),
    }
    # **始终显式传 prompt**(不再空 context 时省略):我们的 _MOSS_DEFAULT_PROMPT 带标点
    # 增补条款,服务端默认没有——省略即丢快语速标点(见常量注释)。context 非空再拼热词后缀。
    data["prompt"] = _MOSS_DEFAULT_PROMPT
    if context and context.strip():
        data["prompt"] = f"{_MOSS_DEFAULT_PROMPT}热词提示:{context.strip()}"
    try:
        resp = await client.post(
            f"{base.rstrip('/')}/v1/audio/transcriptions",
            files=files, data=data, timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise HTTPException(503, detail=f"MOSS ASR 服务不可达: {e}") from e
    if resp.status_code != 200:
        raise HTTPException(
            503, detail=f"MOSS ASR 服务错误 HTTP {resp.status_code}: {resp.text[:200]}",
        )

    body = resp.json()
    language = body.get("language")  # MOSS 未必回语种;没有则 None,字段保留。
    segments: list[dict] = []
    texts: list[str] = []
    for seg in body.get("segments") or []:
        raw = seg.get("text") or ""
        m = _MOSS_SPEAKER_RE.match(raw)
        if m:
            speaker: str | None = m.group(1)
            seg_text = m.group(2).strip()
        else:
            # 防御:段缺 [Sxx] 前缀(格式异常)→ speaker=None,文本原样保留,不崩。
            speaker = None
            seg_text = raw.strip()
        segments.append({
            "start": seg.get("start"),
            "end": seg.get("end"),
            "speaker": speaker,
            "text": seg_text,
        })
        texts.append(seg_text)
    text = "".join(texts)
    return text, language, segments


# --- 标点恢复兜底(PR-10)-----------------------------------------------------
# 快语速 / 无停顿口播下 MOSS 有时整篇 0 标点(prompt steer 已试、真实音频无效:94s「烫嘴」
# 教程音频 10 段全文零标点;正常语速音频标点齐全)。转写后若标点密度异常低 → 用本机常驻
# LLM 做**纯标点恢复**(只加标点、严禁改字),校验后回填;任何失败静默降级用原文。加标点
# 是质量增强、非结构变更(契约 §1 只加不改允许);标点恢复绝不拖垮主转写路径。
_PUNCT_CHARS = frozenset("，。？！、；,.?!;")  # 中英:逗/句/问/叹/顿/分号
_PUNCT_MIN_CHARS = 80              # 全文 < 此长度不触发(短文本密度不稳,兜底无意义)
_PUNCT_DENSITY_THRESHOLD = 0.005  # 标点数/字符数 < 此值判「异常低」→ 触发恢复
# 标点 LLM(env 可覆盖)= 本机常驻 qwen3。**只读选址、绝不按需拉起**(见 _resolve_punct_base_url)。
_PUNCT_LLM_ENGINE_DEFAULT = "qwen3_8_27b_abliterated_awq"


def _punctuation_density(text: str) -> float:
    """标点密度 = 标点字符数 / 总字符数(标点集见 `_PUNCT_CHARS`:中英逗/句/问/叹/顿/分号)。
    空文本 → 0.0。用于判定 MOSS 是否输出了近乎零标点的文本(快语速失效场景)。"""
    if not text:
        return 0.0
    n = sum(1 for c in text if c in _PUNCT_CHARS)
    return n / len(text)


def _strip_punct(s: str) -> str:
    """剥掉标点集里的所有标点字符(校验回填用:剥标点后逐字对比,防 LLM 幻觉改字)。"""
    return "".join(c for c in s if c not in _PUNCT_CHARS)


def _resolve_punct_base_url(model_mgr) -> str | None:
    """标点 LLM 选址:`NOUS_PUNCT_LLM_ENGINE`(默认 `qwen3_8_27b_abliterated_awq`)。

    用只读的 `get_vllm_base_url`:只查 `is_loaded`、取 `base_url`,**不触发任何加载**
    (2026-09-05 spec §4 起全数据面都只有这一条路,懒加载变体已删)。标点恢复只是纯增强,
    绝不能为它把主转写路径拖成几十秒冷启动。engine 未加载 / 无端点 /
    model_manager 不可用 → 返回 None(调用方静默降级用原文)。
    """
    engine = os.environ.get("NOUS_PUNCT_LLM_ENGINE") or _PUNCT_LLM_ENGINE_DEFAULT
    try:
        return get_vllm_base_url(model_mgr, engine).rstrip("/")
    except (VLLMNotLoaded, VLLMNoEndpoint):
        return None


_PUNCT_SYS_PROMPT = (
    "你是中文文本标点恢复助手。用户会给出多行编号文本,每行格式为 `<行号>|<文本>`。"
    "你的唯一任务:只为每行中文文本添加标点符号(，。？！、；),"
    "严禁增、删、改任何一个字,严禁合并或拆分行,严禁改动行号。"
    "请按完全相同的 `<行号>|<文本>` 格式逐行原样返回全部行,不要输出任何额外说明或代码块。"
)

_PUNCT_LINE_RE = re.compile(r"^\s*(\d+)\s*\|(.*)$")


async def _restore_punctuation(
    client: httpx.AsyncClient, base_url: str, segments: list[dict],
) -> list[dict] | None:
    """用本机常驻 LLM 为 segments 做纯标点恢复,返回新 segments(text 已回填标点)或 None。

    一次 chat/completions:把每段编成 `<n>|<text>` 行(n=0..N-1)喂 LLM,指令只加标点、原样
    返回。temperature 0、max_tokens=总字数*1.3+256、超时 min(60,总字数/50+10)s。
    **校验后回填(防 LLM 幻觉)**:逐行解析 `<n>|<text>`;行数不齐 / 编号对不上 → 整体放弃
    (None);对每行剥掉标点后必须与原段文本剥标点后逐字相等,不等的行保留原文,通过的行用
    恢复文本。任何异常 / 非 200 / 超时 → None(静默降级,绝不拖垮主路)。不 mutate 入参。
    """
    texts = [(s.get("text") or "") for s in segments]
    total_chars = sum(len(t) for t in texts)
    if total_chars == 0:
        return None
    numbered = "\n".join(f"{i}|{t}" for i, t in enumerate(texts))
    body = {
        "model": "",  # vLLM 用自身 served 模型(同 chat/embeddings 的处理)
        "messages": [
            {"role": "system", "content": _PUNCT_SYS_PROMPT},
            {"role": "user", "content": numbered},
        ],
        "temperature": 0,
        "max_tokens": int(total_chars * 1.3) + 256,
        # qwen3 等思考模型:关思考 —— 否则推理 token 撑爆 max_tokens 截断真正答案,且徒增延迟。
        "chat_template_kwargs": {"enable_thinking": False},
    }
    timeout = min(60.0, total_chars / 50 + 10)
    try:
        resp = await client.post(
            f"{base_url.rstrip('/')}/v1/chat/completions", json=body, timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        content = resp.json()["choices"][0]["message"]["content"] or ""
    except Exception:  # noqa: BLE001 — 标点恢复是增强,任何失败都静默降级用原文
        return None

    parsed: list[tuple[int, str]] = []
    for line in content.splitlines():
        m = _PUNCT_LINE_RE.match(line)
        if m:
            parsed.append((int(m.group(1)), m.group(2).strip()))
    if len(parsed) != len(segments):
        return None  # 行数不齐 → 整体放弃

    out: list[dict] = []
    for i, (seg, (n, restored)) in enumerate(zip(segments, parsed)):
        if n != i:
            return None  # 编号对不上 → 整体放弃
        orig = seg.get("text") or ""
        # 剥标点逐字对比:相等才采用恢复文本(只多了标点);不等(LLM 改/删/增了字)保留原文。
        if _strip_punct(restored).strip() == _strip_punct(orig).strip():
            out.append({**seg, "text": restored})
        else:
            out.append({**seg, "text": orig})
    return out


def _detect_language(text: str) -> str | None:
    """纯字符集启发式的文本语种检测(**无第三方依赖**),返回 ISO 639-1 码。

    引擎不回语种时(MOSS 现状)由它兜底,让 `language` 出真值而非恒 null。按 Unicode 区段
    逐字符统计归属,覆盖 zh / ja / ko / ru / en:
    - 平假名 / 片假名(kana)是日语独有 → 有 kana 即判 `ja`(**优先于 CJK**,日文汉字混排
      不误判 zh);
    - 谚文(hangul)→ `ko`;
    - 否则在 CJK(`zh`)/ 西里尔(`ru`)/ 拉丁及其余字母(`en` 兜底)间取**字符数最多**者;
    - 无任何可判定字母(纯数字 / 标点 / 空)→ `None`(调用方据此保持 null 或落 `"und"`)。
    """
    kana = hangul = cjk = cyrillic = latin = 0
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:  # 平假名 / 片假名(含片假名扩展)
            kana += 1
        elif (0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF
              or 0x3130 <= o <= 0x318F):  # 谚文音节 + Jamo + 兼容 Jamo
            hangul += 1
        elif (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
              or 0xF900 <= o <= 0xFAFF):  # CJK 统一表意(含扩展 A / 兼容表意)
            cjk += 1
        elif 0x0400 <= o <= 0x04FF:  # 西里尔
            cyrillic += 1
        elif ch.isalpha():  # 前面已剥离东亚 / 西里尔,余下字母归拉丁(en)兜底桶
            latin += 1
    if kana:
        return "ja"
    if hangul:
        return "ko"
    counts = {"zh": cjk, "ru": cyrillic, "en": latin}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else None


# --- 段合并后处理(可选,merge_segments=true 开启) ---------------------------
# MOSS 分段粒度跟内容节奏走:快节奏口播被切到 ~1.9s/段(12 分钟视频出 ~387 段,几乎
# 一短句一段),对字幕/阅读场景太碎。服务端确定性贪心合并把碎段并成句子级,默认关、
# 契约既有输出零变化(只加不改)。纯函数、不 mutate 入参,好测。
_MERGE_MAX_GAP = 0.8       # s:相邻段间隔 > 此值不合并(节奏断点,保停顿语义)
_MERGE_MIN_DURATION = 3.0  # s:句末标点封组的最小组时长(太短的「一句」不单独成组)
_MERGE_MAX_DURATION = 15.0  # s:组时长硬上限,超过即封组(防字幕块过长)
_MERGE_MAX_CHARS = 80      # 字:组字数硬上限,超过即封组(防单块过长)
# 句末标点:组文本以其一收尾 + 组时长 ≥ MIN_DURATION → 封组(自然句边界)。
_MERGE_SENTENCE_END = ("。", "!", "?", "!", "?", "…")


def _merge_seg_duration(start, end) -> float:
    """组/段时长(秒);start/end 任一缺失(防御,契约保证是 float)→ 0。"""
    if start is None or end is None:
        return 0.0
    return max(0.0, float(end) - float(start))


def _merge_concat_text(a: str, b: str) -> str:
    """拼接两段文本:中文直接串接;若前段结尾与后段开头都是 ASCII 字母/数字则补一个
    空格,防英文单词/数字粘连(`good`+`morning` → `good morning`)。"""
    if a and b and a[-1].isascii() and a[-1].isalnum() and b[0].isascii() and b[0].isalnum():
        return a + " " + b
    return a + b


def _merge_group_should_close(text: str, start, end) -> bool:
    """组是否已「封口」(不再接收后续段)。满足其一即封:
    - 组字数 > 硬上限(80);
    - 组时长 > 硬上限(15s);
    - 组文本以句末标点收尾 **且** 组时长 ≥ 下限(3s)—— 自然句边界。
    speaker 变化 / 间隔超阈是**下一段**触发的封组,不在此(由合并主循环判定)。"""
    if len(text) > _MERGE_MAX_CHARS:
        return True
    dur = _merge_seg_duration(start, end)
    if dur > _MERGE_MAX_DURATION:
        return True
    if dur >= _MERGE_MIN_DURATION and text.rstrip().endswith(_MERGE_SENTENCE_END):
        return True
    return False


def _merge_segments(segments: list[dict]) -> list[dict]:
    """把碎分段贪心顺序合并成句子级(merge_segments=true 时用)。规则见常量注释:

    - 只合并**同 speaker**(均为 None 视为同)且相邻间隔 `next.start - cur.end <=
      _MERGE_MAX_GAP` 的段;
    - 组内文本拼接(`_merge_concat_text`,中文直接串接、英文补空格);
    - 断开条件(满足其一即封组,不再接后续段):组文本句末标点收尾且组时长 ≥ 3s;
      组时长 > 15s;组字数 > 80;speaker 变化或间隔超阈;
    - 合并后段 `{start=组首 start, end=组尾 end, speaker=组 speaker, text=拼接}`;
    - 空列表/单段原样返回;**不 mutate 入参**(输出全是新 dict)。
    """
    if len(segments) <= 1:
        return list(segments)

    out: list[dict] = []
    cur: dict | None = None
    cur_closed = False  # 组已封口(硬上限 / 句末封组)→ 下一段必起新组

    def _flush() -> None:
        nonlocal cur
        if cur is not None:
            out.append({
                "start": cur["start"],
                "end": cur["end"],
                "speaker": cur["speaker"],
                "text": cur["text"],
            })
            cur = None

    for seg in segments:
        s_start = seg.get("start")
        s_end = seg.get("end")
        s_speaker = seg.get("speaker")
        s_text = seg.get("text") or ""

        if cur is None:
            cur = {"start": s_start, "end": s_end, "speaker": s_speaker, "text": s_text}
            cur_closed = _merge_group_should_close(s_text, s_start, s_end)
            continue

        # 间隔超阈?(next.start - cur.end;任一缺时间戳则不按间隔断,交由其它条件)
        gap_over = (
            s_start is not None and cur["end"] is not None
            and float(s_start) - float(cur["end"]) > _MERGE_MAX_GAP
        )
        # 起新组三情形:组已封口 / speaker 变 / 间隔超阈 —— 不并。
        if cur_closed or s_speaker != cur["speaker"] or gap_over:
            _flush()
            cur = {"start": s_start, "end": s_end, "speaker": s_speaker, "text": s_text}
            cur_closed = _merge_group_should_close(s_text, s_start, s_end)
            continue

        # 并入当前组:文本拼接、end 延到本段尾;再判是否封口(供下一段决策)。
        cur["text"] = _merge_concat_text(cur["text"], s_text)
        cur["end"] = s_end
        cur_closed = _merge_group_should_close(cur["text"], cur["start"], cur["end"])

    _flush()
    return out


# response_format 白名单(OpenAI 兼容)。json/text = v1 契约语义;verbose_json = 契约 §4
# 演进钩子(→ OpenAI-Whisper 段形状)。未知值 → 400。
_ASR_RESPONSE_FORMATS = ("json", "text", "verbose_json")


def _asr_verbose_json(
    text: str, language: str | None, segments: list[dict], audio_seconds: int,
) -> dict:
    """把归一化后的 (text, language, segments) 映射成 OpenAI-Whisper `verbose_json` 形状。

    契约 §4 的「严格 OpenAI-Whisper 兼容走 response_format=verbose_json 开新分支映射」在此实现:
    默认输出(text 首字段 + language + usage + 平台增强 segments)一字不动,verbose_json 是
    **另一条出参分支**,OpenAI SDK(openai-python `verbose_json`)可直连。

    - `language`:MOSS 无语种识别时(None)填 `"und"`(OpenAI 语义:未定语种;不缺字段)。
    - `duration`:= 自算 `audio_seconds`(float 化)。**取值来源**:`_asr_moss_transcribe` 只回
      `(text, language, segments)` 三元组、**不透传** MOSS 响应里的 `duration`,而 `audio_seconds`
      是全局计费/超时/max_new_tokens 的单一秒数口径(自归一化 wav 算),复用它保持一致、不引入
      第二个时长来源。
    - `segments[].{seek,tokens,temperature,avg_logprob,compression_ratio,no_speech_prob}`:**中性
      占位** —— MOSS 0.9B 不产这些 token 级/逐段置信度指标,给 OpenAI SDK 惯例默认值(字段在、
      值中性),避免 SDK 因缺字段报错。
    - `segments[].speaker`:nous **附加字段**(OpenAI schema 无此键),SDK 忽略未知字段;无说话人
      归属的段为 `null`(与 v1 契约 speaker nullability 一致)。
    """
    verbose_segments = [
        {
            "id": i,
            "seek": 0,
            "start": seg.get("start"),
            "end": seg.get("end"),
            "text": seg.get("text", ""),
            "tokens": [],
            "temperature": 0.0,
            "avg_logprob": 0.0,
            "compression_ratio": 1.0,
            "no_speech_prob": 0.0,
            "speaker": seg.get("speaker"),
        }
        for i, seg in enumerate(segments)
    ]
    return {
        "task": "transcribe",
        "language": language if language is not None else "und",
        "duration": float(audio_seconds),
        "text": text,
        "segments": verbose_segments,
    }


@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str | None = Form(None),
    language: str | None = Form(None),
    response_format: str | None = Form(None),
    context: str | None = Form(None),
    timestamps: bool = Form(False),
    merge_segments: bool = Form(False),
    punctuate: bool = Form(True),
    auth: tuple[ServiceInstance | None, InstanceApiKey | None] = Depends(_auth_bearer_or_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """OpenAI 兼容语音转写(ASR)。

    multipart:`file`(音频)+ `model`(服务名)+ 可选 `context`(领域/热词偏置)。解析链同
    /v1/embeddings:model → 服务(M:N grant)→ source_type=model。上传音频先 ffmpeg 归一化。

    2026-07-20 起后端切 **MOSS-Transcribe-Diarize SGLang 微服务**(退役 Qwen3-ASR vLLM chat
    路径 + nous-aligner:MOSS 内建时间戳 + 说话人分离;spec 2026-07-20-moss-asr-sglang-serving)。
    默认 / `response_format=json`:返回 `{text, language, usage:{type:"duration",seconds}}`
    (text 仍首字段,纯文本客户端不受影响);`timestamps=true` 时加 `segments`(段级
    {start,end,speaker,text},不再有 words 字段)。`response_format=text`:纯文本 body
    (text/plain,OpenAI 语义,无 JSON 包裹)。`response_format=verbose_json`:OpenAI-Whisper
    段形状(契约 §4,`_asr_verbose_json`;**隐含分段**,始终带 segments,无需另传 timestamps)。
    未知 `response_format` → 400。秒数自算(归一化 wav)。计量/任务中心记录不受 format 影响。
    出参 `language`:引擎回了用引擎值,没回(MOSS 现状)退回文本字符集检测(`_detect_language`,
    出 zh/ja/... ISO 码),都判不出才 null(verbose_json 落 "und")。入参 `language` 仍为 OpenAI
    兼容保留位(不消费)。Bearer 走 grant+quota;admin cookie(Playground)直查服务、跳配额。
    MOSS 不可达 → 503(唯一 ASR 主路)。可选 `merge_segments=true`:把 MOSS 碎分段服务端
    确定性合并成句子级(字幕/阅读场景),作用于 segments、两种输出格式一处生效;text 全文
    不变,默认关(既有输出零变化)。可选 `punctuate`(默认 **true**):快语速 MOSS 出零标点时
    (len(text)≥80 且标点密度<0.005)自动用本机常驻 LLM 做纯标点恢复(只加标点、严禁改字,
    校验后回填;LLM 未加载/不可达/校验失败静默降级用原文),在 merge_segments 之前生效。
    """
    instance, api_key = auth
    requested_model = model or None
    admin_run = api_key is None  # admin session 旁路(Playground)

    # response_format 消费(契约 §4):校验取值(未知 → 400,纯客户端错误,早于任何 MOSS 调用/
    # 任务落库,不噪 failed task);归一化小写。verbose_json 隐含分段。
    fmt = (response_format or "json").strip().lower()
    if fmt not in _ASR_RESPONSE_FORMATS:
        raise InvalidRequestError(
            f"unsupported response_format '{response_format}'; "
            f"expected one of {', '.join(_ASR_RESPONSE_FORMATS)}",
            code="unsupported_response_format",
        )

    if admin_run:
        # admin:按服务名直查(单管理员隐式授权,跳 grant/quota),同 /v1/apps execute_service。
        instance = await _admin_lookup_service(session, requested_model)
    else:
        try:
            instance = await resolve_target_service(
                session, api_key=api_key, requested_model=requested_model,
            )
        except ModelNotFound as e:
            raise NotFoundError(str(e), code="model_not_found")
        if instance.status != "active":
            raise HTTPException(403, detail="Instance is inactive")
        from src.api.deps_auth import enforce_instance_rate_limit
        await enforce_instance_rate_limit(instance)
        await _preflight_quota(session, api_key.id, instance.id)

    if instance.source_type != "model":
        raise HTTPException(
            400,
            detail=f"transcriptions 只支持 model-backed 服务(got source_type={instance.source_type})",
        )

    engine_name = instance.source_name or str(instance.source_id)
    key_id = api_key.id if api_key else None
    # Arc 3(spec §8):直连转写调用进任务中心 —— 入参快照(回显)+ 整调用计时。
    # PR-9:两段式 —— 归一化后 create running(转写期间任务中心即可见「正在转写」),
    # 结束翻 completed/failed。
    from src.services.api_call_tasks import (
        create_api_call_task,
        finalize_api_call_task,
    )
    task_input = {
        "model": requested_model,
        "timestamps": timestamps,
        "context": context,
        "filename": file.filename or None,
    }
    req_start = time.monotonic()

    # —— 选址 + 读入 + ffmpeg 归一化(纯输入校验,失败早于 running task 建立 → 直接抛、
    #    不噪 failed task,同 response_format 400 的口径)——
    # MOSS 引擎选址(Arc 2):env override > ModelManager(resident;2026-09-05 起只读,
    # 未加载即 503,不在请求路径上拉起)。
    model_mgr = getattr(request.app.state, "model_manager", None)
    try:
        moss_base_url = _resolve_moss_base_url(model_mgr, engine_name)
    except VLLMNotLoaded as e:
        # 2026-09-05 spec §5:与 chat/embeddings 同一信封 —— 「已授权但未加载」在整个
        # 数据面都是 503 model_not_ready(裸 HTTPException(503) 会落 type=api_error
        # code=null,下游拿不到 code 也拿不到指向 /v1/models 的 fix)。
        raise ModelNotReadyError(engine_name) from e
    except VLLMNoEndpoint as e:
        raise HTTPException(500, detail=str(e)) from e

    raw = await file.read()
    if not raw:
        raise InvalidRequestError("空音频文件")
    wav = await _ffmpeg_to_wav16k(raw)
    # MOSS 不回音频秒数 → 自算(归一化 wav 16k/mono/s16le);计量 + max_new_tokens + 超时都按它。
    audio_seconds = _wav16k_seconds(wav)

    # 归一化后即建 running task —— audio_seconds 已知,input_json.kind=asr 让卡片即时派生
    # type=asr + 时长(serialize 的 running 态兜底)。create 失败回 None,finalize 短路。
    task_id = await create_api_call_task(
        service_name=instance.name,  # 用户视角服务名(moss-asr),非引擎名 —— 任务卡显示用
        api_key_id=key_id,
        input_meta={**task_input, "kind": "asr", "audio_seconds": audio_seconds},
    )

    try:
        start_ms = time.monotonic()
        async with httpx.AsyncClient(proxy=None) as client:
            # MOSS 微服务:一次拿到 文本 + 段级时间戳 + 说话人分离(不可达 → 503)。
            text, detected_language, segments = await _asr_moss_transcribe(
                client, wav, context, audio_seconds, moss_base_url,
            )
    except Exception as exc:
        # 转写失败(503/解析异常):running task 翻 failed,error 带简短原因;不吞异常(照抛)。
        detail = getattr(exc, "detail", None) or str(exc)
        await finalize_api_call_task(
            task_id,
            status="failed",
            duration_ms=int((time.monotonic() - req_start) * 1000),
            error=str(detail)[:200],
        )
        raise

    # 标点恢复兜底(PR-10,默认开、punctuate=false 关):快语速 MOSS 出零标点时,用本机
    # 常驻 LLM 做纯标点恢复。**必须在 merge_segments 之前** —— 合并的封组断句规则依赖句末
    # 标点(_MERGE_SENTENCE_END),顺序反了则合并拿到的还是零标点碎段、断句失效。触发条件:
    # len(text) >= 80 且密度 < 0.005。MOSS verbose_json 始终有 segments,统一在 segments 上
    # 恢复(timestamps=false 也生效),再由各段 text 拼回顶层 text。选址只读、未加载即放弃,
    # LLM 不可达/校验失败 → 静默降级用原文(restored is None)。
    if (
        punctuate and segments
        and len(text) >= _PUNCT_MIN_CHARS
        and _punctuation_density(text) < _PUNCT_DENSITY_THRESHOLD
    ):
        punct_base = _resolve_punct_base_url(model_mgr)
        if punct_base:
            async with httpx.AsyncClient(proxy=None) as pclient:
                restored = await _restore_punctuation(pclient, punct_base, segments)
            if restored is not None:
                segments = restored
                text = "".join((s.get("text") or "") for s in segments)

    # merge_segments(可选,默认关):把 MOSS 碎分段服务端确定性合并成句子级(字幕/阅读
    # 场景)。作用在归一化 segments 之后、两种输出格式(默认 + verbose_json)之前,一处
    # 生效两格式。text 全文不变(全文本来就是全段拼接,合并只并段边界)。任务中心记录的
    # segments_count 也用合并后数量(用户视角一致)。
    if merge_segments and segments:
        segments = _merge_segments(segments)

    duration_ms = int((time.monotonic() - start_ms) * 1000)
    from src.services.usage_service import record_llm_usage
    await record_llm_usage(
        model=engine_name,
        prompt_tokens=0,
        completion_tokens=0,
        duration_ms=duration_ms,
        instance_id=instance.id,
        api_key_id=key_id,
        agent_id=None,
    )
    # 计量:按音频秒数扣(ASR 无 token 概念);至少 1。admin(Playground)跳过 grant/quota。
    if api_key is not None:
        await _post_consume_quota(api_key.id, instance.id, max(1, audio_seconds))
    # 成功:running task 翻 completed(await —— 写一行很快,任务中心即时更新)。text 预览截
    # 120 字符;speakers 去重排序;result 的 audio_seconds 是前端派生 task_type=asr 的判据。
    speakers = sorted({s.get("speaker") for s in segments if s.get("speaker")})
    await finalize_api_call_task(
        task_id,
        status="completed",
        duration_ms=int((time.monotonic() - req_start) * 1000),
        result={
            "text": text[:120] if isinstance(text, str) else "",
            "segments_count": len(segments),
            "speakers": speakers,
            "audio_seconds": audio_seconds,
        },
    )
    # language:引擎回了(未来引擎)优先用引擎值;没回(MOSS 现状)退回文本字符集检测,
    # 让两种格式都出真值(zh/ja/... ISO 码),都判不出才是 None。
    out_language = detected_language or _detect_language(text)

    # response_format 出参映射(契约 §4)。计量/任务中心已按 v1 口径记完,不受 format 影响。
    if fmt == "text":
        # OpenAI text 语义:纯文本 body(text/plain),无 JSON 包裹、无 language/usage/segments。
        return Response(content=text, media_type="text/plain; charset=utf-8")
    if fmt == "verbose_json":
        # OpenAI-Whisper 形状:隐含分段,始终带 segments(不看 timestamps);language None → "und"。
        return _asr_verbose_json(text, out_language, segments, audio_seconds)
    # 默认 / json:v1 契约不变 —— text 仍是首要字段(纯文本客户端不受影响);
    # language/segments 是增量字段,segments 仅 timestamps=true 时出现。
    out: dict = {
        "text": text,
        "language": out_language,
        "usage": {"type": "duration", "seconds": audio_seconds},
    }
    if timestamps:
        # 段级 diar 结果(MOSS 内建,{start,end,speaker,text});不再有字级 words。
        out["segments"] = segments
    return out


# --- /v1/images/generations ---


class ImageGenerationRequest(BaseModel):
    # extra="allow":火山式额外参数(如 SeedVR2 的 resolution)随 body 透传,
    # 按服务 exposed_inputs 的 key 通用合并注入(见 handler)。
    model_config = ConfigDict(extra="allow")

    model: str = Field(..., description="已发布的 image 服务名(= ServiceInstance.name)")
    # prompt 可选:图生图/编辑有 prompt,但纯超分(SeedVR2 细节增强)无 prompt。
    prompt: str | None = None
    # 输入图(图生图/编辑/超分):base64 data URI('data:image/...;base64,...')。
    # 对齐火山 Seedream:image 字段接 URL 或 base64;本轮先吃 base64(URL 下载留 follow-up)。
    # 多图传 list(火山多参考),当前 image_input 节点只消费单图。
    image: str | list[str] | None = None
    n: int = 1
    # OpenAI 兼容字段。当前工作流用自身固定尺寸,size 暂作占位(不注入);
    # response_format 先只支持 url(b64_json 留待后续读图字节编码)。
    size: str | None = None
    response_format: Literal["url", "b64_json"] = "url"


def _pick_prompt_input_key(exposed_inputs: list | None) -> str | None:
    """选承接 prompt 的 exposed input key:优先 string/text 类型,否则第一个。"""
    if not exposed_inputs:
        return None
    for p in exposed_inputs:
        if str(p.get("type", "")).lower() in ("string", "text", "str"):
            return p.get("key") or p.get("api_name")
    first = exposed_inputs[0]
    return first.get("key") or first.get("api_name")


# 输入源节点:其 output 含 image_url 但那是上传图的回显(image_input executor 落盘签 URL),
# 不是生成结果。外部端点扫 result 捞图时必须跳过,否则把输入图误当输出返回(#372 的外部路径版)。
_INPUT_SOURCE_NODE_TYPES = {"image_input"}


def _input_source_node_ids(snapshot: dict | None) -> set[str]:
    """从 snapshot 找出输入源节点 id(api-shape dict / editor-shape list 都认)。"""
    if not isinstance(snapshot, dict):
        return set()
    ids: set[str] = set()
    nodes = snapshot.get("nodes")
    if isinstance(nodes, dict):
        for nid, n in nodes.items():
            t = (n.get("class_type") or n.get("type")) if isinstance(n, dict) else None
            if t in _INPUT_SOURCE_NODE_TYPES:
                ids.add(str(nid))
    elif isinstance(nodes, list):
        for n in nodes:
            if not isinstance(n, dict):
                continue
            if (n.get("type") or n.get("class_type")) in _INPUT_SOURCE_NODE_TYPES:
                ids.add(str(n.get("id")))
    return ids


def _extract_image_urls(
    result: dict,
    snapshot: dict | None = None,
    exposed_outputs: list | None = None,
) -> list[str]:
    """从 executor result 捞产图终端的 image_url。

    1) 优先按 exposed_outputs 声明的 node_id 取(发布契约把输出指向产图终端
       dec=flux2_vae_decode / up=seedvr2_upscale)——精确、不依赖遍历顺序。
    2) 兜底扫全部节点 output,但跳过 image_input 类型节点的 echo(否则把上传图
       当输出返回,#372 外部路径版)。snapshot 缺省时退化为「扫全部」,与老服务兼容。
    """
    outputs = result.get("outputs", {}) if isinstance(result, dict) else {}
    if not isinstance(outputs, dict):
        return []

    declared = [
        str(p.get("node_id")) for p in (exposed_outputs or [])
        if isinstance(p, dict) and p.get("node_id") is not None
    ]
    # batch(num_images>1):节点 output 带 image_urls 列表(全部 N 张);否则单 image_url。
    def _node_urls(node_out: object) -> list[str]:
        if not isinstance(node_out, dict):
            return []
        many = node_out.get("image_urls")
        if isinstance(many, list) and many:
            return [u for u in many if isinstance(u, str) and u]
        one = node_out.get("image_url")
        return [one] if isinstance(one, str) and one else []

    urls: list[str] = []
    for nid in declared:
        urls.extend(_node_urls(outputs.get(nid)))
    if urls:
        return urls

    input_ids = _input_source_node_ids(snapshot)
    for nid, node_out in outputs.items():
        if str(nid) in input_ids:
            continue
        urls.extend(_node_urls(node_out))
    return urls


@router.post("/v1/images/generations")
async def images_generations(
    body: ImageGenerationRequest,
    request: Request,
    auth: tuple[ServiceInstance | None, InstanceApiKey] = Depends(verify_bearer_token_any),
    session: AsyncSession = Depends(get_async_session),
):
    """OpenAI / 火山(Ark)兼容的图像生成端点。

    统一模态端点 + body.model 选模型 + API key grant 做 scope —— 跟
    /v1/chat/completions 同一套设计(对齐火山:不是每个出图工作流一个 URL
    路径,而是 model 参数指定服务 + key 的授权范围决定可访问哪些)。

    内部 dispatch:body.model = 已发布 image 工作流服务名;body 里命中服务
    exposed_inputs key 的字段(prompt/image/resolution...)通用合并注入(文生图
    =prompt;编辑/角度=image+prompt;超分=image+resolution 无 prompt);经共享
    执行核心 run_published_workflow(带 GPU runner_clients)跑出图;产图终端
    节点的 image_url 转成 OpenAI {data:[{url}]}。
    """
    from sqlalchemy import select
    from sqlalchemy.orm import undefer

    from src.models.api_gateway import ApiKeyGrant
    from src.services.workflow_service_runner import run_published_workflow

    _instance, api_key = auth
    if api_key is None:
        raise NotFoundError("request requires an API key", code="model_not_found")

    # resolve image 服务:(key 的 active grant, model == service name) —— 与
    # chat/completions 同款 M:N scope,这里直接 join + undefer 工作流快照。
    stmt = (
        select(ServiceInstance)
        .options(
            undefer(ServiceInstance.workflow_snapshot),
            undefer(ServiceInstance.exposed_inputs),
            undefer(ServiceInstance.exposed_outputs),
        )
        .join(ApiKeyGrant, ApiKeyGrant.service_id == ServiceInstance.id)
        .where(
            ApiKeyGrant.api_key_id == api_key.id,
            ApiKeyGrant.status == "active",
            ServiceInstance.name == body.model,
        )
    )
    svc = (await session.execute(stmt)).scalar_one_or_none()
    if svc is None:
        raise NotFoundError(
            f"no active grant for model '{body.model}' on this key",
            code="model_not_found",
        )

    # 通用参数合并(火山式):body 里任意字段命中服务 exposed_inputs 的 key → 注入对应
    # 节点。prompt / image / resolution / negative_prompt 走同一套,SeedVR2 无 prompt 也
    # 不报错。这取代了原「只塞单个文本 prompt」的逻辑(那条让带图/无 prompt 的服务发不出去)。
    exposed = svc.exposed_inputs or []
    exposed_keys = {(p.get("key") or p.get("api_name")) for p in exposed}
    exposed_keys.discard(None)
    body_fields = body.model_dump(exclude_none=True)  # 含 extra 透传字段(resolution 等)
    inputs: dict = {k: body_fields[k] for k in exposed_keys if k in body_fields}

    # OpenAI 兼容兜底:服务的文本输入 key 不字面叫 'prompt' 时,把 body.prompt 注进文本输入。
    if body.prompt is not None:
        prompt_key = _pick_prompt_input_key(exposed)
        if prompt_key and prompt_key not in inputs:
            inputs[prompt_key] = body.prompt

    if not inputs:
        raise InvalidRequestError(
            f"service '{body.model}' received no inputs matching its exposed schema "
            f"(exposed keys: {sorted(k for k in exposed_keys)})",
            code="no_matching_input",
        )

    # 在执行前抓出 snapshot / exposed_outputs —— run_published_workflow 内部多次 commit
    # 会 expire ORM 属性,事后再访问会触发 lazy 重载 → MissingGreenlet。
    snapshot = svc.workflow_snapshot
    out_params = svc.exposed_outputs

    # OpenAI n:一次出 N 张 —— 注入到喂输出的末段采样节点 num_images(batch,B1 全栈)。
    # 段路(非 euler 采样器手写循环)暂只 1 张(引擎层 follow-up),其余路径真出 N 张。
    result = await run_published_workflow(
        request, session, svc, inputs, api_key, num_images=max(1, int(body.n)),
    )

    urls = _extract_image_urls(result, snapshot, out_params)
    if not urls:
        raise APIError(
            f"service '{body.model}' did not produce an image",
            code="no_image_output",
        )

    base = str(request.base_url).rstrip("/")
    abs_urls = [u if u.startswith("http") else base + u for u in urls]
    # 真出了 N 张就返 N 张;截到 n 作上限保护(避免工作流意外多产)。
    data = [{"url": u} for u in abs_urls[: max(1, int(body.n))]]
    return {"created": int(time.time()), "data": data}


# --- /v1/models ---

class ModelObject(BaseModel):
    id: str
    object: str = "model"
    created: int = 1700000000
    owned_by: str = "nous-engine"
    type: str = "model"   # 服务类目:llm / embedding / image / app / tts / vl ...
    # 2026-09-24:给 nous-app admin 自动同步「上下文窗口」用,免人工填。
    # context_window = 生效上下文(yaml 叠运行时覆盖 = vLLM 实际按它启动的值),不是模型原生上限;
    # 非模型服务(工作流 / 图像 / app)为 None。capabilities 与管理面 /api/v1/services 同一份。
    context_window: int | None = None
    capabilities: dict[str, Any] | None = None
    # 现在就能调吗(模型类服务 = 其模型已加载)。不带 include_unready 时列出的恒为 True。
    ready: bool = True


def _model_object(svc: ServiceInstance, configs: dict, ready: bool) -> "ModelObject":
    from src.services.model_capabilities import capabilities_for_service  # noqa: PLC0415
    caps = capabilities_for_service(svc, configs)
    return ModelObject(
        id=svc.name, type=svc.category or "model",
        context_window=(caps or {}).get("context"), capabilities=caps, ready=ready,
    )


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelObject]


async def _granted_services(session: AsyncSession, api_key: InstanceApiKey):
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


@router.get("/v1/models", response_model=ModelListResponse)
async def list_models(
    request: Request,
    type: str | None = None,
    include_unready: bool = False,
    auth: tuple[ServiceInstance | None, InstanceApiKey] = Depends(verify_bearer_token_any),
    session: AsyncSession = Depends(get_async_session),
):
    """List the services THIS key can call (OpenAI 兼容发现端点)。

    返回该 key **active-grant 的全部服务**(LLM / embedding / 图像工作流 / app / TTS…),
    `id` = **服务名**(与 /v1/chat·/v1/embeddings·/v1/images 的 `model` 字段完全一致),
    `type` = 类目(客户端据此选端点)。即「发现到的 == 能调的」—— 对齐 Doubao 式
    一链多服务自选。可选 `?type=llm` 过滤类目。

    2026-09-05 起 model 类服务只在其模型已加载时出现(`/v1/models` = 现在就能调的);
    comfy_template / workflow / app 照旧按授权列。

    `?include_unready=1`(2026-09-24,给 nous-app 自动同步):**只放宽就绪、不放宽授权** ——
    返回该 key 授权的全部服务(含模型未加载的),每条带 `ready`。不带时行为与原来完全一致。
    每条都带 `context_window` + `capabilities`(来自配置,与加载与否无关)。
    """
    _instance, api_key = auth
    if api_key is None:
        raise NotFoundError("request requires an API key", code="model_not_found")
    from src.api.routes import _readiness  # noqa: PLC0415 — 模块引用,测试按属性打桩
    from src.config import load_model_configs  # noqa: PLC0415
    model_mgr = getattr(request.app.state, "model_manager", None)
    services = await _granted_services(session, api_key)
    configs = load_model_configs()
    data = []
    for s in services:
        if type and (s.category or "model") != type:
            continue
        ready = _readiness.service_is_ready(model_mgr, s)
        if not ready and not include_unready:   # spec 2026-09-05 §6:默认只列现在就能调的
            continue
        data.append(_model_object(s, configs, ready))
    return ModelListResponse(data=data)


@router.get("/v1/models/{model_id}")
async def get_model(
    model_id: str,
    request: Request,
    include_unready: bool = False,
    auth: tuple[ServiceInstance | None, InstanceApiKey] = Depends(verify_bearer_token_any),
    session: AsyncSession = Depends(get_async_session),
):
    """Get one service the key can call (OpenAI 兼容)。按服务名查,未授权 → 404。"""
    _instance, api_key = auth
    if api_key is None:
        raise NotFoundError("request requires an API key", code="model_not_found")
    svc = next((s for s in await _granted_services(session, api_key) if s.name == model_id), None)
    if svc is None:
        raise NotFoundError(
            f"model '{model_id}' not found or no active grant on this key",
            code="model_not_found")
    from src.api.routes import _readiness  # noqa: PLC0415 — 模块引用,测试按属性打桩
    from src.config import load_model_configs  # noqa: PLC0415
    ready = _readiness.service_is_ready(getattr(request.app.state, "model_manager", None), svc)
    if not ready and not include_unready:
        raise ModelNotReadyError(model_id)      # 「没就绪」(503)与「没授权」(404)分开
    return _model_object(svc, load_model_configs(), ready)
