"""skill-runs 纯逻辑(spec 2026-09-26-skill-runs-orchestration-design)。

无 I/O。只认「一段 Skill 指令 + 一段用户输入 → 一段文本」与「服务 input schema 里的文本字段」,
不认识任何具体模型、工作流或其内部结构 —— 那些是模板管理数据,不进这里。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.errors import BadGatewayError, UnprocessableError
from src.services.service_schema import FILE_INPUT_TYPES, input_key
from src.utils.frontmatter import FrontmatterError, split_frontmatter

# 未指定 max_tokens 时的默认值;路由层还会按模型 max_model_len 夹紧。
DEFAULT_PREVIEW_MAX_TOKENS = 2048


@dataclass(frozen=True)
class ParsedSkill:
    name: str | None
    instructions: str


class InvalidPromptFieldError(UnprocessableError):
    """prompt_field 不是该服务可承接自由文本的输入 —— 错误体带可选清单,调用方可自查。"""

    def __init__(self, prompt_field: str, text_fields: list[str]):
        super().__init__(
            f"prompt_field {prompt_field!r} 不是该服务的文本输入",
            code="invalid_prompt_field",
            param="prompt_field",
            fix="从 error.text_fields 里选一个;GET /v1/services/{name}/schema 可看完整契约",
        )
        self.text_fields = list(text_fields)

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["error"]["text_fields"] = list(self.text_fields)
        return d


def parse_skill(content: str) -> ParsedSkill:
    """SKILL.md 全文 → (name, 指令正文)。正文为空或 frontmatter 非法 → 422。"""
    try:
        fm, body = split_frontmatter(content)
    except FrontmatterError as e:
        raise UnprocessableError(
            str(e), code="skill_invalid_frontmatter", param="skill.content") from e
    instructions = body.strip()
    if not instructions:
        raise UnprocessableError(
            "SKILL 正文为空", code="skill_empty", param="skill.content")
    name = fm.get("name")
    return ParsedSkill(name=str(name) if name is not None else None, instructions=instructions)


def _is_empty_input(user_input: str | list[dict[str, Any]]) -> bool:
    if isinstance(user_input, str):
        return not user_input.strip()
    return len(user_input) == 0


# 预览只放行文本与图片两类 content part;其余(video_url / audio_url / 无 type …)
# 一律 422,不透传给上游 —— 这些类型绕过图片 URL 的 SSRF 校验。
_ALLOWED_PART_TYPES = ("text", "image_url")
# 单个 part 的尺寸上限(整体只限了 32 个 part,不限单个就等于没限):文本 100k 字符;
# image_url(多为 data: URL)16 MiB 字符 ≈ 12 MB 图片,远大于实际出图/编辑原图(~3 MB)。
MAX_TEXT_PART_CHARS = 100_000
MAX_IMAGE_URL_CHARS = 16 * 1024 * 1024


def validate_content_parts(parts: list[dict[str, Any]]) -> None:
    """content parts 白名单 + 单 part 尺寸上限(preview 与 /v1/rerank 共用)。"""
    for i, part in enumerate(parts):
        if not isinstance(part, dict):
            raise _bad_part(f"input[{i}] 不是对象")
        ptype = part.get("type")
        if ptype not in _ALLOWED_PART_TYPES:
            raise _bad_part(f"input[{i}] 的 type={ptype!r} 不受支持(只收 text / image_url)")
        if ptype == "text":
            text = part.get("text")
            if not isinstance(text, str):
                raise _bad_part(f"input[{i}] 的 text part 缺少字符串 text")
            if len(text) > MAX_TEXT_PART_CHARS:
                raise _bad_part(f"input[{i}] 的 text 超过 {MAX_TEXT_PART_CHARS} 字符")
        if ptype == "image_url":
            image_url = part.get("image_url")
            if not isinstance(image_url, dict) or not isinstance(image_url.get("url"), str):
                raise _bad_part(f"input[{i}] 的 image_url 必须是含字符串 url 的对象")
            if len(image_url["url"]) > MAX_IMAGE_URL_CHARS:
                raise _bad_part(f"input[{i}] 的 image_url 超过 {MAX_IMAGE_URL_CHARS} 字符(约 12 MB 图片)")


def _bad_part(message: str) -> UnprocessableError:
    return UnprocessableError(message, code="invalid_input_part", param="input")


def build_chat_body(
    skill: ParsedSkill,
    user_input: str | list[dict[str, Any]],
    *,
    temperature: float | None,
    max_tokens: int | None,
    thinking: str,
) -> dict[str, Any]:
    """Skill 指令 = system,调用方输入 = user(字符串或 OpenAI content parts,原样)。"""
    if _is_empty_input(user_input):
        raise UnprocessableError("input 为空", code="empty_input", param="input")
    if isinstance(user_input, list):
        validate_content_parts(user_input)
    body: dict[str, Any] = {
        "model": "",  # vLLM 用自己的模型路径(同 /v1/chat/completions)
        "messages": [
            {"role": "system", "content": skill.instructions},
            {"role": "user", "content": user_input},
        ],
        "max_tokens": max_tokens or DEFAULT_PREVIEW_MAX_TOKENS,
        "thinking": {"type": thinking},
    }
    if temperature is not None:
        body["temperature"] = temperature
    return body


def extract_final_text(data: dict[str, Any]) -> tuple[str, str | None]:
    """chat 响应 → (最终文本, finish_reason)。截断先于空判:半截 prompt 绝不交给工作流。"""
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as e:
        raise BadGatewayError(
            "上游响应缺少 choices", code="skill_bad_upstream_response") from e
    if not isinstance(choice, dict):
        raise BadGatewayError("上游 choice 不是对象", code="skill_bad_upstream_response")
    message = choice.get("message")
    if message is not None and not isinstance(message, dict):
        raise BadGatewayError("上游 message 不是对象", code="skill_bad_upstream_response")
    content = (message or {}).get("content")
    if content is not None and not isinstance(content, str):
        raise BadGatewayError("上游 content 不是字符串", code="skill_bad_upstream_response")
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise BadGatewayError(
            "Skill 输出被 max_tokens 截断;调大 options.max_tokens 或关闭 thinking",
            code="skill_output_truncated",
        )
    text = (content or "").strip()
    if not text:
        raise BadGatewayError("Skill 输出为空", code="skill_empty_output")
    return text, finish_reason


def text_input_fields(input_schema: dict[str, Any], exposed_inputs: list | None) -> list[str]:
    """服务 input schema 里能承接自由文本的字段:string、非文件类、没有静态/动态选项清单。"""
    file_keys = {
        input_key(e) for e in (exposed_inputs or [])
        if str(e.get("type") or "").lower() in FILE_INPUT_TYPES
    }
    props = (input_schema or {}).get("properties") or {}
    return sorted(
        key for key, prop in props.items()
        if prop.get("type") == "string"
        and key not in file_keys
        and "enum" not in prop
        and "x-options-source" not in prop
    )


def merge_prompt_input(
    inputs: dict[str, Any], prompt_field: str, text: str, text_fields: list[str],
) -> dict[str, Any]:
    """返回新 dict `{**inputs, prompt_field: text}`;不改 inputs。"""
    if not text.strip():
        raise UnprocessableError("text 为空", code="empty_text", param="text")
    if prompt_field not in text_fields:
        raise InvalidPromptFieldError(prompt_field, text_fields)
    if prompt_field in inputs:
        raise UnprocessableError(
            f"input 里已有 {prompt_field!r};该字段的值只能来自 text",
            code="prompt_field_conflict",
            param="input",
        )
    return {**inputs, prompt_field: text}
