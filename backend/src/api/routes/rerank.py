"""POST /v1/rerank —— 重排(2026-09-27,Qwen3-VL-Reranker 接入)。

Cohere/Jina 兼容形(= vLLM 原生 /v1/rerank):
    {"model": <rerank 服务名>, "query": <str | {"content": [...]}>,
     "documents": [<str | {"content": [...]}>, ...], "top_n"?: int, "return_documents"?: bool,
     "instruction"?: str, "chat_template_kwargs"?: {"instruction": str}}
→ {"id", "model", "usage", "results": [{"index", "relevance_score", "document"?}, ...]}(按分数降序)。

`content` 只收 text / image_url 两类 part(与 skill-runs preview 同一白名单 + 尺寸上限),
image_url 过与 chat 同一套 SSRF 校验。鉴权 / grant / 限流 / 配额 / 未加载即 503 / 用量记账
全走 chat 的共用层(src/api/chat_invoke.py、src/api/service_access.py)—— 数据面对放置只读。
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.chat_invoke import (
    invoke_chat_nonstream,
    preflight_quota,
    resolve_chat_endpoint,
)
from src.api.service_access import Auth, auth_bearer_or_admin_session, resolve_service_for_call
from src.errors import InvalidRequestError
from src.models.database import get_async_session
from src.services.skill_run import MAX_TEXT_PART_CHARS, validate_content_parts
from src.utils.url_security import UnsafeURLError, validate_chat_media_urls

router = APIRouter(prefix="/v1", tags=["rerank"])

RERANK_CATEGORY = "rerank"
MAX_DOCUMENTS = 256
MAX_CONTENT_PARTS = 32
MAX_INSTRUCTION_CHARS = 4096

Text = Annotated[str, Field(min_length=1, max_length=MAX_TEXT_PART_CHARS)]


class ContentInput(BaseModel):
    content: list[dict[str, Any]] = Field(..., min_length=1, max_length=MAX_CONTENT_PARTS)


RerankInput = Text | ContentInput


class RerankRequest(BaseModel):
    model: str
    query: RerankInput
    documents: list[RerankInput] = Field(..., min_length=1, max_length=MAX_DOCUMENTS)
    top_n: int | None = Field(default=None, gt=0)
    return_documents: bool | None = None
    # 任务指令,经 score 模板的 `instruction` 变量进 <Instruct>(vLLM 同名字段的两种写法都收)。
    # chat_template_kwargs 只放行 instruction —— 其余键会直接喂进 jinja 渲染,没有用途就不开口子。
    instruction: str | None = Field(default=None, min_length=1, max_length=MAX_INSTRUCTION_CHARS)
    chat_template_kwargs: dict[str, str] | None = None

    @model_validator(mode="after")
    def _fold_instruction(self) -> RerankRequest:
        kwargs = self.chat_template_kwargs
        if kwargs is not None:
            extra = set(kwargs) - {"instruction"}
            if extra:
                raise ValueError(f"chat_template_kwargs 只支持 instruction,不支持 {sorted(extra)}")
            inner = kwargs.get("instruction")
            if inner is not None:
                if not 0 < len(inner) <= MAX_INSTRUCTION_CHARS:
                    raise ValueError(f"instruction 长度须在 1..{MAX_INSTRUCTION_CHARS}")
                if self.instruction is not None and self.instruction != inner:
                    raise ValueError("instruction 与 chat_template_kwargs.instruction 不一致")
                self.instruction = inner
        self.chat_template_kwargs = None
        return self

    def upstream_body(self) -> dict[str, Any]:
        """转发给 vLLM 的 body:instruction 统一放进 chat_template_kwargs,model 置空。"""
        body = self.model_dump(mode="json", exclude_none=True, exclude={"instruction"})
        if self.instruction is not None:
            body["chat_template_kwargs"] = {"instruction": self.instruction}
        body["model"] = ""  # vLLM 用自己的 served 模型(同 chat / embeddings)
        return body


def _content_items(body: RerankRequest) -> list[ContentInput]:
    return [x for x in (body.query, *body.documents) if isinstance(x, ContentInput)]


@router.post("/rerank", response_model=None)
async def rerank(
    body: RerankRequest,
    request: Request,
    auth: Auth = Depends(auth_bearer_or_admin_session),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any] | Response:
    """对 documents 按与 query 的相关性打分并降序返回。"""
    items = _content_items(body)
    for item in items:
        validate_content_parts(item.content)
    try:
        await validate_chat_media_urls([{"content": item.content} for item in items])
    except UnsafeURLError as e:
        raise InvalidRequestError(str(e), code="unsafe_image_url", param="documents") from e

    instance, api_key = await resolve_service_for_call(session, auth, body.model)
    if instance.source_type != "model" or instance.category != RERANK_CATEGORY:
        raise InvalidRequestError(
            f"service {body.model!r} 不是 rerank 模型(category={instance.category})",
            code="not_a_rerank_model", param="model",
        )
    if api_key is not None:
        await preflight_quota(session, api_key.id, instance.id)

    model_mgr = getattr(request.app.state, "model_manager", None)
    endpoint = await resolve_chat_endpoint(
        session, model_mgr=model_mgr, instance=instance, api_key=api_key,
        requested_model=body.model,
    )
    result = await invoke_chat_nonstream(
        model_mgr=model_mgr, endpoint=endpoint, body=body.upstream_body(),
        instance=instance, api_key=api_key, path="/v1/rerank",
    )
    if result.data is None:  # 上游非 200:原样透出(同 chat)
        return Response(content=result.content, status_code=result.status_code,
                        media_type="application/json")
    return {**result.data, "model": body.model}
