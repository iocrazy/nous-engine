"""Run image-prompt skills and submit their output to workflow services."""

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator

from src.api.deps_auth import verify_bearer_token_any
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance
from src.services import skill_manager

router = APIRouter(prefix="/v1/skill-runs", tags=["skill-runs"])


class InlineSkill(BaseModel):
    name: str = Field(default="inline", min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    body: str = Field(min_length=1, max_length=65536)


class PromptRequest(BaseModel):
    model: str = Field(default="qwen3-8-27b", min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=32768)
    skill_name: str | None = Field(default=None, min_length=1, max_length=128)
    skill: InlineSkill | None = None
    images: list[str] = Field(default_factory=list, max_length=4)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1, le=32768)

    @model_validator(mode="after")
    def validate_skill_source(self):
        if (self.skill_name is None) == (self.skill is None):
            raise ValueError("provide exactly one of skill_name or skill")
        return self


class GenerationTarget(BaseModel):
    service: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)
    prompt_field: str = Field(default="prompt", min_length=1, max_length=128)


class GenerateRequest(BaseModel):
    prompt: str | None = Field(default=None, min_length=1, max_length=65536)
    prompt_request: PromptRequest | None = None
    generation: GenerationTarget

    @model_validator(mode="after")
    def validate_prompt_source(self):
        if (self.prompt is None) == (self.prompt_request is None):
            raise ValueError("provide exactly one of prompt or prompt_request")
        return self


def _load_skill(body: PromptRequest) -> dict[str, str]:
    if body.skill is not None:
        return {
            "name": body.skill.name,
            "description": body.skill.description,
            "body": body.skill.body,
            "source": "inline",
        }
    try:
        stored = skill_manager.get_skill(body.skill_name or "")
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, detail="skill not found") from exc
    return {
        "name": stored["name"],
        "description": stored.get("description", ""),
        "body": stored["body"],
        "source": "nous-engine",
    }


def _chat_body(body: PromptRequest, skill: dict[str, str]) -> dict[str, Any]:
    system = (
        "Execute the image-generation skill below. Follow its instructions exactly. "
        "Return only the final generation prompt, without Markdown fences, headings, "
        "analysis, or explanation.\n\n"
        f"Skill: {skill['name']}\n"
        f"Description: {skill['description']}\n\n"
        f"{skill['body']}"
    )
    if body.images:
        user_content: str | list[dict[str, Any]] = [
            {"type": "text", "text": body.instruction},
            *(
                {"type": "image_url", "image_url": {"url": image}}
                for image in body.images
            ),
        ]
    else:
        user_content = body.instruction
    return {
        "model": body.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": body.temperature,
        "max_tokens": body.max_tokens,
        "stream": False,
    }


def _error_detail(response: httpx.Response) -> Any:
    try:
        data = response.json()
    except ValueError:
        return response.text[:500] or "upstream request failed"
    if isinstance(data, dict):
        return data.get("detail", data.get("error", data))
    return data


async def _preview(body: PromptRequest, request: Request) -> dict[str, Any]:
    skill = _load_skill(body)
    headers = {"Authorization": request.headers["authorization"]}
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://nous.internal", timeout=320,
    ) as client:
        result = await client.post(
            "/v1/chat/completions", json=_chat_body(body, skill), headers=headers,
        )
    if result.status_code >= 400:
        raise HTTPException(result.status_code, detail=_error_detail(result))
    data = result.json()
    try:
        prompt = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError, TypeError) as exc:
        raise HTTPException(502, detail="model returned no prompt") from exc
    if not prompt:
        raise HTTPException(502, detail="model returned an empty prompt")
    return {
        "prompt": prompt,
        "model": body.model,
        "skill": {"name": skill["name"], "source": skill["source"]},
    }


@router.post("/preview")
async def preview_skill_prompt(
    body: PromptRequest,
    request: Request,
    _auth: tuple[ServiceInstance | None, InstanceApiKey] = Depends(verify_bearer_token_any),
):
    return await _preview(body, request)


@router.post("/generate")
async def generate_from_skill(
    body: GenerateRequest,
    request: Request,
    response: Response,
    prefer: str | None = Header(default=None),
    _auth: tuple[ServiceInstance | None, InstanceApiKey] = Depends(verify_bearer_token_any),
):
    if body.prompt_request is not None:
        preview = await _preview(body.prompt_request, request)
        prompt = preview["prompt"]
    else:
        prompt = body.prompt or ""

    generation_input = dict(body.generation.input)
    generation_input[body.generation.prompt_field] = prompt
    headers = {"Authorization": request.headers["authorization"]}
    if prefer:
        headers["Prefer"] = prefer
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://nous.internal", timeout=620,
    ) as client:
        result = await client.post(
            f"/v1/services/{body.generation.service}/predictions",
            json={"input": generation_input},
            headers=headers,
        )
    if result.status_code >= 400:
        raise HTTPException(result.status_code, detail=_error_detail(result))
    response.status_code = result.status_code
    return {"prompt": prompt, "prediction": result.json()}
