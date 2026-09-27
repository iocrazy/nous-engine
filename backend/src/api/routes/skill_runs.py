"""Skill 驱动的工作流编排(spec 2026-09-26-skill-runs-orchestration-design)。

- POST /v1/skill-runs/preview:用指定 Chat 模型执行调用方内联的 SKILL.md,返回最终文本。
- POST /v1/skill-runs/generate:把最终文本写进调用方指定的 prompt_field,提交任意已发布工作流。

本模块只认「服务名 + 输入 key」,不认识任何具体模型、工作流或其内部结构 —— 那些是模板
管理数据。数据面:对模型放置只读(未加载即 503,绝不加载)。
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.chat_invoke import (
    clamped_max_tokens,
    inject_thinking,
    invoke_chat_nonstream,
    preflight_quota,
    resolve_chat_endpoint,
)
from src.api.prediction_submit import check_submittable, submit_prediction
from src.api.service_access import Auth, auth_bearer_or_admin_session, resolve_service_for_call
from src.errors import InvalidRequestError
from src.models.database import get_async_session
from src.services.service_schema import build_service_io_schema
from src.services.skill_run import (
    build_chat_body,
    extract_final_text,
    merge_prompt_input,
    parse_skill,
    text_input_fields,
)
from src.utils.url_security import UnsafeURLError, validate_chat_media_urls

router = APIRouter(prefix="/v1/skill-runs", tags=["skill-runs"])

_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


class SkillSpec(BaseModel):
    content: str = Field(..., max_length=200_000)


class PreviewOptions(BaseModel):
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    # 默认关思考:思考 token 计入 max_tokens,写 prompt 类 Skill 开思考极易截断。
    thinking: Literal["enabled", "disabled", "auto"] = "disabled"


# 输入上限:防一个请求把超大文本/海量 part 塞给上游(超限走 400 validation_error)。
MAX_INPUT_CHARS = 100_000
MAX_INPUT_PARTS = 32


class PreviewRequest(BaseModel):
    model: str
    skill: SkillSpec
    # part 的 type / 字段形状由 build_chat_body 白名单校验(422 invalid_input_part)
    input: (
        Annotated[str, Field(max_length=MAX_INPUT_CHARS)]
        | Annotated[list[dict[str, Any]], Field(max_length=MAX_INPUT_PARTS)]
    )
    options: PreviewOptions = Field(default_factory=PreviewOptions)


class GenerateRequest(BaseModel):
    service: str
    prompt_field: str
    text: str = Field(..., max_length=MAX_INPUT_CHARS)
    input: dict[str, Any] = Field(default_factory=dict)
    webhook: str | None = None
    webhook_events_filter: list[str] | None = None


@router.post("/preview", response_model=None)
async def preview(
    body: PreviewRequest,
    request: Request,
    auth: Auth = Depends(auth_bearer_or_admin_session),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any] | Response:
    """用 Chat 模型执行 Skill,返回最终文本。"""
    skill = parse_skill(body.skill.content)
    chat_body = build_chat_body(
        skill, body.input,
        temperature=body.options.temperature,
        max_tokens=body.options.max_tokens,
        thinking=body.options.thinking,
    )
    try:
        await validate_chat_media_urls(chat_body["messages"])
    except UnsafeURLError as e:
        raise InvalidRequestError(str(e), code="unsafe_image_url", param="input") from e

    instance, api_key = await resolve_service_for_call(session, auth, body.model)
    if instance.source_type != "model":
        raise InvalidRequestError(
            f"service {body.model!r} 不是 Chat 模型(source_type={instance.source_type})",
            code="not_a_chat_model", param="model",
        )
    if api_key is not None:
        await preflight_quota(session, api_key.id, instance.id)

    model_mgr = getattr(request.app.state, "model_manager", None)
    endpoint = await resolve_chat_endpoint(
        session, model_mgr=model_mgr, instance=instance, api_key=api_key,
        requested_model=body.model,
    )
    chat_body = {
        **chat_body,
        "max_tokens": clamped_max_tokens(chat_body["max_tokens"], endpoint.max_model_len),
    }
    inject_thinking(chat_body, endpoint.engine_name)  # 原地:thinking → chat_template_kwargs
    result = await invoke_chat_nonstream(
        model_mgr=model_mgr, endpoint=endpoint, body=chat_body,
        instance=instance, api_key=api_key,
    )
    if result.data is None:  # 上游非 200:原样透出(同 /v1/chat/completions)
        return Response(content=result.content, status_code=result.status_code,
                        media_type="application/json")
    text, finish_reason = extract_final_text(result.data)
    usage = result.data.get("usage") or {}
    return {
        "text": text,
        "model": body.model,
        "skill": {"name": skill.name},
        "usage": {k: usage.get(k, 0) for k in _USAGE_KEYS},
        "finish_reason": finish_reason,
    }


@router.post("/generate", response_model=None)
async def generate(
    body: GenerateRequest,
    request: Request,
    response: Response,
    prefer: str | None = Header(default=None),
    auth: Auth = Depends(auth_bearer_or_admin_session),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """把最终文本写进 prompt_field,提交任意已发布工作流;返回 Prediction(同 predictions)。"""
    instance, api_key = await resolve_service_for_call(session, auth, body.service)
    check_submittable(instance)  # 先于字段校验:给 model 服务的是「走 chat」而不是「字段不对」
    schema = build_service_io_schema(
        instance.exposed_inputs, instance.exposed_outputs, instance.workflow_snapshot)
    inputs = merge_prompt_input(
        body.input, body.prompt_field, body.text,
        text_input_fields(schema["input_schema"], instance.exposed_inputs),
    )
    result = await submit_prediction(
        session, app_state=request.app.state, instance=instance, api_key=api_key,
        inputs=inputs, prefer=prefer,
        webhook=body.webhook, webhook_events_filter=body.webhook_events_filter,
    )
    response.status_code = result.status_code
    return result.prediction
