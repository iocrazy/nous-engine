# skill-runs 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现通用的 `POST /v1/skill-runs/preview`(Chat 模型执行内联 SKILL.md → 最终文本)与 `POST /v1/skill-runs/generate`(文本写入 `prompt_field` → 提交任意已发布工作流),并以 Qwen Image 2.1 文生图 / 图像编辑做端到端验证。

**Architecture:** 把 `/v1/chat/completions` 的非流式核心抽到 `src/api/chat_invoke.py`、把 `/v1/services/{name}/predictions` 的提交核心抽到 `src/api/prediction_submit.py`、鉴权+服务解析抽到 `src/api/service_access.py`;老路由与新 `src/api/routes/skill_runs.py` 共用它们。编排专属的纯逻辑在 `src/services/skill_run.py`(无 I/O、不认识任何具体服务)。

**Tech Stack:** FastAPI、Pydantic v2、SQLAlchemy async、httpx、pytest + pytest-asyncio(PostgreSQL 临时库)。

**Spec:** `docs/superpowers/specs/2026-09-26-skill-runs-orchestration-design.md`

## Global Constraints

- `src/services/skill_run.py` 与 `src/api/routes/skill_runs.py` 源码(含注释/docstring)**不得出现** `qwen`、`comfy`、`node_id`(不区分大小写)。Task 4 有静态测试锁定。
- 数据面对模型放置只读:新代码不得出现 `ensure_vllm_base_url`、`.load_model(`、`.get_loaded_adapter(`、`get_or_load`;未加载 → 503 `model_not_ready`。
- 重构(Task 2、3)**行为不变**:既有测试的行为断言一律不改;只允许改「按源码位置断言/打桩」的测试路径(计划里点名的两处)。
- 函数签名全带类型注解,用 `X | None`;值对象用 `@dataclass(frozen=True)`;不原地改入参 dict(新代码)。
- 注释语言跟随仓库:中文为主。
- Commit 格式 `<type>: <description>`(feat/fix/refactor/docs/test/chore),无 attribution。
- 生产检出是主仓库:**只在 worktree `.claude/worktrees/skill-runs`(分支 `feat/skill-runs`)里干活**,不碰主仓库的分支与文件。

## 环境准备(每个执行者开工前一次)

```bash
cd /media/heygo/program/projects-code/repos/nous-engine/.claude/worktrees/skill-runs/backend
uv sync --frozen            # worktree 自己的 .venv(与 CI 同:不装 inference extra,不碰生产 venv)
export DATABASE_URL="$(grep '^DATABASE_URL=' /media/heygo/program/projects-code/repos/nous-engine/backend/.env | cut -d= -f2-)"
nvidia-smi >/dev/null && echo gpu-ok   # CLAUDE.md:跑测试前确认驱动活着
```

所有 `uv run pytest` 命令都在 `backend/` 下、带上面的 `DATABASE_URL` 执行。**同一时间只允许一个人跑全量**。

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/src/errors.py` | 改 | 加 `UnprocessableError`(422)、`BadGatewayError`(502) |
| `backend/src/utils/frontmatter.py` | 建 | `split_frontmatter(raw) -> (dict, body)` + `FrontmatterError` |
| `backend/src/services/skill_manager.py` | 改 | `_parse_frontmatter` 改调 `split_frontmatter` |
| `backend/src/services/service_schema.py` | 改 | 公开别名 `FILE_INPUT_TYPES`、`input_key` |
| `backend/src/services/skill_run.py` | 建 | 纯逻辑:`parse_skill` / `build_chat_body` / `extract_final_text` / `text_input_fields` / `merge_prompt_input` / `InvalidPromptFieldError` |
| `backend/src/api/chat_invoke.py` | 建 | `resolve_chat_endpoint` / `clamped_max_tokens` / `inject_thinking` / `preflight_quota` / `post_consume_quota` / `granted_services` / `invoke_chat_nonstream` |
| `backend/src/api/routes/openai_compat.py` | 改 | 搬走上述函数(留同名私有别名),chat 非流式分支改调 `invoke_chat_nonstream` |
| `backend/src/api/service_access.py` | 建 | `auth_bearer_or_admin_session` / `resolve_service_for_call` |
| `backend/src/api/prediction_submit.py` | 建 | `parse_prefer` / `check_submittable` / `submit_prediction` / `SubmitResult` |
| `backend/src/api/routes/predictions.py` | 改 | 改调上两者 |
| `backend/src/api/routes/skill_runs.py` | 建 | 两个端点 |
| `backend/src/api/main.py` | 改 | 注册 router |
| `backend/tests/test_skill_run.py` | 建 | 纯逻辑单测 |
| `backend/tests/test_chat_invoke.py` | 建 | `clamped_max_tokens` 单测 + 别名存在 |
| `backend/tests/test_skill_runs_preview.py` | 建 | preview 端点 |
| `backend/tests/test_skill_runs_generate.py` | 建 | generate 端点 |
| `backend/tests/test_skill_runs_generic.py` | 建 | 静态守卫:不含 qwen/comfy/node_id |
| `backend/tests/test_data_plane_readonly.py` | 改 | 加 skill-runs 路径的无加载能力断言 |
| `backend/tests/test_playground_admin_session_llm.py` | 改 | 打桩点加 `chat_invoke.get_vllm_base_url` |
| `backend/tests/test_prediction_service_pr2.py` | 改 | 源码 grep 改读 `prediction_submit.py` |
| `backend/tests/manual/skill_runs/qwen-image-t2i.SKILL.md` | 建 | E2E 素材 |
| `backend/tests/manual/skill_runs/qwen-image-edit.SKILL.md` | 建 | E2E 素材 |
| `backend/tests/manual/verify_skill_runs.py` | 建 | 真机 E2E 脚本 |
| `docs/skill-runs.md` | 建 | nous-app 接入说明 |
| `CLAUDE.md` | 改 | 数据面清单 + skill-runs 不变式 |

---

### Task 1: 纯逻辑层(errors / frontmatter / skill_run)

**Files:**
- Modify: `backend/src/errors.py`(在 `ServiceUnavailableError` 类之后追加)
- Create: `backend/src/utils/frontmatter.py`
- Modify: `backend/src/services/skill_manager.py:35-46`
- Modify: `backend/src/services/service_schema.py`(文件末尾追加两个别名)
- Create: `backend/src/services/skill_run.py`
- Test: `backend/tests/test_skill_run.py`

**Interfaces:**
- Produces:
  - `src.errors.UnprocessableError(NousError)`:`type="invalid_request_error"`, `http_status=422`
  - `src.errors.BadGatewayError(NousError)`:`type="upstream_error"`, `http_status=502`
  - `src.utils.frontmatter.split_frontmatter(raw: str) -> tuple[dict, str]`,`FrontmatterError(ValueError)`
  - `src.services.service_schema.FILE_INPUT_TYPES: set[str]`,`input_key(exposed: dict) -> str | None`
  - `src.services.skill_run`:
    - `DEFAULT_PREVIEW_MAX_TOKENS = 2048`
    - `@dataclass(frozen=True) ParsedSkill(name: str | None, instructions: str)`
    - `parse_skill(content: str) -> ParsedSkill`
    - `build_chat_body(skill: ParsedSkill, user_input: str | list[dict], *, temperature: float | None, max_tokens: int | None, thinking: str) -> dict`
    - `extract_final_text(data: dict) -> tuple[str, str | None]`
    - `text_input_fields(input_schema: dict, exposed_inputs: list | None) -> list[str]`
    - `merge_prompt_input(inputs: dict, prompt_field: str, text: str, text_fields: list[str]) -> dict`
    - `InvalidPromptFieldError(UnprocessableError)`,属性 `text_fields: list[str]`,`to_dict()` 在 `error` 里加 `text_fields`

- [ ] **Step 1: 写失败测试** `backend/tests/test_skill_run.py`

```python
"""skill_run 纯逻辑(spec 2026-09-26 skill-runs §3/§4)。无 I/O,不需要 DB。"""
from __future__ import annotations

import pytest

from src.errors import BadGatewayError, UnprocessableError
from src.services.skill_run import (
    DEFAULT_PREVIEW_MAX_TOKENS,
    InvalidPromptFieldError,
    ParsedSkill,
    build_chat_body,
    extract_final_text,
    merge_prompt_input,
    parse_skill,
    text_input_fields,
)
from src.utils.frontmatter import FrontmatterError, split_frontmatter

# ---------- frontmatter ----------

def test_split_frontmatter_with_yaml():
    fm, body = split_frontmatter("---\nname: a\n---\n\n正文\n")
    assert fm == {"name": "a"}
    assert body == "正文\n"


def test_split_frontmatter_without_frontmatter_returns_raw():
    assert split_frontmatter("只有正文") == ({}, "只有正文")


def test_split_frontmatter_rejects_non_mapping():
    with pytest.raises(FrontmatterError):
        split_frontmatter("---\n- a\n- b\n---\nx")


def test_split_frontmatter_rejects_bad_yaml():
    with pytest.raises(FrontmatterError):
        split_frontmatter("---\nname: [unclosed\n---\nx")


# ---------- parse_skill ----------

def test_parse_skill_reads_name_and_strips_body():
    s = parse_skill("---\nname: t2i\ndescription: d\n---\n\n  把想法扩写成提示词。 \n")
    assert s == ParsedSkill(name="t2i", instructions="把想法扩写成提示词。")


def test_parse_skill_without_frontmatter_has_no_name():
    assert parse_skill("直接写指令").name is None


def test_parse_skill_empty_body_is_422():
    with pytest.raises(UnprocessableError) as ei:
        parse_skill("---\nname: x\n---\n   \n")
    assert ei.value.code == "skill_empty"
    assert ei.value.http_status == 422


def test_parse_skill_bad_frontmatter_is_422():
    with pytest.raises(UnprocessableError) as ei:
        parse_skill("---\n- a\n---\nbody")
    assert ei.value.code == "skill_invalid_frontmatter"


# ---------- build_chat_body ----------

_SKILL = ParsedSkill(name="s", instructions="SYS")


def test_build_chat_body_string_input():
    body = build_chat_body(_SKILL, "猫", temperature=0.3, max_tokens=100, thinking="disabled")
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "猫"},
    ]
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 100
    assert body["thinking"] == {"type": "disabled"}
    assert body["model"] == ""


def test_build_chat_body_defaults():
    body = build_chat_body(_SKILL, "猫", temperature=None, max_tokens=None, thinking="auto")
    assert "temperature" not in body
    assert body["max_tokens"] == DEFAULT_PREVIEW_MAX_TOKENS


def test_build_chat_body_content_parts_passthrough():
    parts = [
        {"type": "text", "text": "改背景"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    body = build_chat_body(_SKILL, parts, temperature=None, max_tokens=None, thinking="auto")
    assert body["messages"][1] == {"role": "user", "content": parts}


@pytest.mark.parametrize("empty", ["", "   ", []])
def test_build_chat_body_empty_input_is_422(empty):
    with pytest.raises(UnprocessableError) as ei:
        build_chat_body(_SKILL, empty, temperature=None, max_tokens=None, thinking="auto")
    assert ei.value.code == "empty_input"


# ---------- extract_final_text ----------

def _reply(content, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


def test_extract_final_text_strips():
    assert extract_final_text(_reply("  a cat  \n")) == ("a cat", "stop")


def test_extract_final_text_truncated_is_502_even_with_text():
    with pytest.raises(BadGatewayError) as ei:
        extract_final_text(_reply("half a prom", finish="length"))
    assert ei.value.code == "skill_output_truncated"
    assert ei.value.http_status == 502


@pytest.mark.parametrize("content", ["", "  ", None])
def test_extract_final_text_empty_is_502(content):
    with pytest.raises(BadGatewayError) as ei:
        extract_final_text(_reply(content))
    assert ei.value.code == "skill_empty_output"


def test_extract_final_text_malformed_upstream_is_502():
    with pytest.raises(BadGatewayError) as ei:
        extract_final_text({"choices": []})
    assert ei.value.code == "skill_bad_upstream_response"


# ---------- text_input_fields / merge_prompt_input ----------

_SCHEMA = {"type": "object", "properties": {
    "caption": {"type": "string"},
    "ref": {"type": "string"},                          # 文件类(见 exposed type)
    "style": {"type": "string", "enum": ["a", "b"]},    # 选项字段,不承接自由文本
    "pack": {"type": "string", "x-options-source": "x"},
    "steps": {"type": "integer"},
    "notes": {"type": "string"},
}}
_EXPOSED = [
    {"key": "caption", "type": "string"},
    {"key": "ref", "type": "image"},
    {"key": "style", "type": "string"},
    {"key": "pack", "type": "string"},
    {"key": "steps", "type": "integer"},
    {"key": "notes", "type": "string"},
]


def test_text_input_fields_only_free_text_strings():
    assert text_input_fields(_SCHEMA, _EXPOSED) == ["caption", "notes"]


def test_merge_prompt_input_injects_without_mutating():
    original = {"steps": 20}
    merged = merge_prompt_input(original, "caption", "a cat", ["caption", "notes"])
    assert merged == {"steps": 20, "caption": "a cat"}
    assert original == {"steps": 20}


@pytest.mark.parametrize("text", ["", "   "])
def test_merge_prompt_input_empty_text(text):
    with pytest.raises(UnprocessableError) as ei:
        merge_prompt_input({}, "caption", text, ["caption"])
    assert ei.value.code == "empty_text"


@pytest.mark.parametrize("field", ["missing", "ref", "steps", "style"])
def test_merge_prompt_input_invalid_field_lists_text_fields(field):
    with pytest.raises(InvalidPromptFieldError) as ei:
        merge_prompt_input({}, field, "x", ["caption", "notes"])
    err = ei.value
    assert err.code == "invalid_prompt_field"
    assert err.http_status == 422
    assert err.to_dict()["error"]["text_fields"] == ["caption", "notes"]


def test_merge_prompt_input_conflict():
    with pytest.raises(UnprocessableError) as ei:
        merge_prompt_input({"caption": "old"}, "caption", "new", ["caption"])
    assert ei.value.code == "prompt_field_conflict"
```

- [ ] **Step 2: 跑,确认失败**

Run: `uv run pytest tests/test_skill_run.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'src.services.skill_run'`

- [ ] **Step 3: 实现**

`backend/src/errors.py` —— 在 `class ServiceUnavailableError` 定义块之后追加:

```python
class UnprocessableError(NousError):
    """422 — 请求结构合法,但语义上不可用(空 Skill、字段选错等)。"""

    type = "invalid_request_error"
    http_status = 422


class BadGatewayError(NousError):
    """502 — 上游答了,但答案不可用(空输出、被截断、结构不对)。"""

    type = "upstream_error"
    http_status = 502
```

`backend/src/utils/frontmatter.py`:

```python
"""YAML frontmatter 切分(`---\\n<yaml>\\n---\\n正文` 格式,SKILL.md 等用)。"""
from __future__ import annotations

import yaml


class FrontmatterError(ValueError):
    """frontmatter 存在,但不是合法的 YAML 映射。"""


def split_frontmatter(raw: str) -> tuple[dict, str]:
    """→ (frontmatter dict, 正文)。没有 frontmatter 时返回 ({}, raw)。"""
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as e:
        raise FrontmatterError(f"frontmatter 不是合法 YAML: {e}") from e
    if not isinstance(fm, dict):
        raise FrontmatterError("frontmatter 必须是 YAML 映射(key: value)")
    return fm, parts[2].lstrip("\n")
```

`backend/src/services/skill_manager.py` —— 把 `_parse_frontmatter` 整个函数体替换为:

```python
def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """Split SKILL.md into (frontmatter_dict, body_text)."""
    from src.utils.frontmatter import split_frontmatter

    return split_frontmatter(raw)
```

`backend/src/services/service_schema.py` —— 文件末尾追加:

```python
# 公开别名:skill_run 等调用方用,不直接 import 私有名。
FILE_INPUT_TYPES = _FILE_IN_TYPES
input_key = _input_key
```

`backend/src/services/skill_run.py`:

```python
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
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        raise BadGatewayError(
            "Skill 输出被 max_tokens 截断;调大 options.max_tokens 或关闭 thinking",
            code="skill_output_truncated",
        )
    text = ((choice.get("message") or {}).get("content") or "").strip()
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
```

- [ ] **Step 4: 跑,确认通过;跑 skill_manager 回归**

Run: `uv run pytest tests/test_skill_run.py tests/test_api_skills.py -q`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/errors.py backend/src/utils/frontmatter.py backend/src/services/skill_manager.py \
  backend/src/services/service_schema.py backend/src/services/skill_run.py backend/tests/test_skill_run.py
git commit -m "feat(skill-runs): 纯逻辑层 —— Skill 解析、chat body、最终文本、prompt_field 校验"
```

---

### Task 2: 抽 `src/api/chat_invoke.py`(行为不变的重构)

**Files:**
- Create: `backend/src/api/chat_invoke.py`
- Modify: `backend/src/api/routes/openai_compat.py`(`_THINKING_MODEL_PATTERNS`…`_maybe_inject_thinking` 约 92-175 行、`_granted_services` 约 1606-1623 行、chat_completions 的 readiness 段约 268-287 行与 clamp/非流式段约 393-535 行)
- Modify: `backend/tests/test_playground_admin_session_llm.py:26-36`
- Test: `backend/tests/test_chat_invoke.py`

**Interfaces:**
- Produces(`src.api.chat_invoke`):
  - `supports_thinking(engine_name: str) -> bool`
  - `inject_thinking(body: dict, engine_name: str) -> None`(原 `_maybe_inject_thinking`,原地改 body —— 保持原行为)
  - `async preflight_quota(session: AsyncSession, api_key_id: int, service_id: int) -> None`
  - `async post_consume_quota(api_key_id: int, service_id: int, units: int) -> None`
  - `async granted_services(session: AsyncSession, api_key: InstanceApiKey) -> Sequence[ServiceInstance]`
  - `@dataclass(frozen=True) ChatEndpoint(engine_name: str, base_url: str, max_model_len: int)`
  - `async resolve_chat_endpoint(session, *, model_mgr, instance: ServiceInstance, api_key: InstanceApiKey | None, requested_model: str | None) -> ChatEndpoint`(未加载 → `ModelNotReadyError`;无端点 → `HTTPException(500)`)
  - `clamped_max_tokens(requested: int | None, max_model_len: int) -> int | None`
  - `@dataclass(frozen=True) ChatHttpResult(status_code: int, content: bytes, data: dict | None)`(`data` 仅 200 时非 None)
  - `async invoke_chat_nonstream(*, model_mgr, endpoint: ChatEndpoint, body: dict, instance: ServiceInstance, api_key: InstanceApiKey | None, agent_id: str | None = None) -> ChatHttpResult`
- `openai_compat` 保留旧私有名作别名:`_supports_thinking`、`_maybe_inject_thinking`、`_preflight_quota`、`_post_consume_quota`、`_granted_services`、`_THINKING_MODEL_PATTERNS`(`ollama_compat`、`responses`、`tests/test_thinking_mapping.py` 仍从这里 import)。

- [ ] **Step 1: 写失败测试** `backend/tests/test_chat_invoke.py`

```python
"""chat_invoke:从 openai_compat 抽出的非流式 chat 共用核心(spec 2026-09-26 skill-runs §4)。"""
from __future__ import annotations

import pytest

from src.api.chat_invoke import clamped_max_tokens


@pytest.mark.parametrize(("requested", "mml", "expected"), [
    (None, 4096, None),        # 没给就不设
    (100, 4096, 100),          # 没超不动
    (3584, 4096, 3584),        # 恰好 = mml-512 不动
    (4000, 4096, 3584),        # 超了夹到 mml-512
    (4000, 800, 400),          # mml-512 < mml//2 时取 mml//2
])
def test_clamped_max_tokens(requested, mml, expected):
    assert clamped_max_tokens(requested, mml) == expected


def test_openai_compat_keeps_private_aliases():
    """ollama_compat / responses / 既有测试仍从 openai_compat import 这些名字。"""
    from src.api import chat_invoke
    from src.api.routes import openai_compat as oc
    assert oc._maybe_inject_thinking is chat_invoke.inject_thinking
    assert oc._supports_thinking is chat_invoke.supports_thinking
    assert oc._post_consume_quota is chat_invoke.post_consume_quota
    assert oc._preflight_quota is chat_invoke.preflight_quota
    assert oc._granted_services is chat_invoke.granted_services
```

- [ ] **Step 2: 跑,确认失败**

Run: `uv run pytest tests/test_chat_invoke.py -q`
Expected: `ModuleNotFoundError: No module named 'src.api.chat_invoke'`

- [ ] **Step 3: 建 `backend/src/api/chat_invoke.py`**

把 `openai_compat.py` 里的 `_THINKING_MODEL_PATTERNS`、`_supports_thinking`、`_preflight_quota`、`_post_consume_quota`、`_maybe_inject_thinking`、`_granted_services` **连同其注释原样剪切**过来,改成下面的公开名;再加新函数。完整文件:

```python
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
# (原 openai_compat 注释原样保留)
_THINKING_MODEL_PATTERNS = (
    # ← 原样粘贴 openai_compat 里的元组内容
)


def supports_thinking(engine_name: str) -> bool:
    n = (engine_name or "").lower()
    return any(p in n for p in _THINKING_MODEL_PATTERNS)


def inject_thinking(body: dict, engine_name: str) -> None:
    # ← 原 _maybe_inject_thinking 的 docstring + 函数体原样粘贴,
    #    其中 `_supports_thinking(` 改为 `supports_thinking(`
    ...


async def preflight_quota(session: AsyncSession, api_key_id: int, service_id: int) -> None:
    # ← 原 _preflight_quota 原样粘贴
    ...


async def post_consume_quota(api_key_id: int, service_id: int, units: int) -> None:
    # ← 原 _post_consume_quota 原样粘贴(docstring 一并)
    ...


async def granted_services(
    session: AsyncSession, api_key: InstanceApiKey,
) -> Sequence[ServiceInstance]:
    # ← 原 _granted_services 原样粘贴
    ...


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
        from src.api.routes._readiness import ready_model_names  # noqa: PLC0415
        raise ModelNotReadyError(
            requested_model or engine_name,
            ready_models=ready_model_names(
                model_mgr, await granted_services(session, api_key) if api_key else []),
        ) from e
    except VLLMNoEndpoint as e:
        raise HTTPException(500, detail=str(e)) from e
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
        if model_mgr is not None:
            model_mgr.remove_reference(endpoint.engine_name, proxy_ref)
```

(`# ← 原样粘贴` 处必须是真实剪切过来的代码,提交物里不得留这些占位注释与 `...`。)

- [ ] **Step 4: 改 `openai_compat.py`**

4a. 删掉已搬走的 6 个定义,在 import 区(`from src.api.deps_auth import verify_bearer_token_any` 之后)加:

```python
from src.api.chat_invoke import (
    _THINKING_MODEL_PATTERNS,  # noqa: F401 — 旧 import 路径兼容
    clamped_max_tokens,
    invoke_chat_nonstream,
    resolve_chat_endpoint,
)
from src.api.chat_invoke import granted_services as _granted_services
from src.api.chat_invoke import inject_thinking as _maybe_inject_thinking
from src.api.chat_invoke import post_consume_quota as _post_consume_quota
from src.api.chat_invoke import preflight_quota as _preflight_quota
from src.api.chat_invoke import supports_thinking as _supports_thinking  # noqa: F401
```

4b. chat_completions 里,把从 `engine_name = instance.source_name or str(instance.source_id)` 到 `adapter = model_mgr.get_adapter(engine_name)` 这一整段(含 try/except VLLMNotLoaded/VLLMNoEndpoint)替换为:

```python
    # spec §4.5 D6/D8: direct-to-vLLM HTTP;未就绪即刻 503(数据面对放置只读,见 chat_invoke)。
    model_mgr = getattr(request.app.state, "model_manager", None)
    endpoint = await resolve_chat_endpoint(
        session, model_mgr=model_mgr, instance=instance, api_key=api_key,
        requested_model=body.get("model"),
    )
    engine_name, base_url = endpoint.engine_name, endpoint.base_url
```

4c. 把 `# Clamp max_tokens` 那三行替换为:

```python
    # Clamp max_tokens
    clamped = clamped_max_tokens(body.get("max_tokens"), endpoint.max_model_len)
    if clamped is not None:
        body["max_tokens"] = clamped
```

4d. 把 `proxy_ref = f"proxy-{uuid.uuid4().hex}"` 及其 `add_reference` 两行(连同上方 C3 注释)**移进** `if is_stream:` 分支开头(非流式由 `invoke_chat_nonstream` 自己加引用)。

4e. 把 `else:` 非流式分支整体替换为:

```python
    else:
        result = await invoke_chat_nonstream(
            model_mgr=model_mgr, endpoint=endpoint, body=body,
            instance=instance, api_key=api_key,
            agent_id=agent_id if settings.NOUS_ENABLE_AGENT_INJECTION else None,
        )
        return Response(
            content=result.content, status_code=result.status_code,
            media_type="application/json",
        )
```

4f. 自检:`grep -n "adapter\b\|VLLMNotLoaded\|VLLMNoEndpoint\|uuid\." backend/src/api/routes/openai_compat.py` —— chat_completions 内不应再有 `adapter` 引用;`VLLMNotLoaded` / `get_vllm_base_url` 若仍被 embeddings 等其它端点使用则保留 import,否则删掉未用 import(`uv run ruff check src/api/routes/openai_compat.py src/api/chat_invoke.py` 必须干净)。

- [ ] **Step 5: 改打桩点** `backend/tests/test_playground_admin_session_llm.py` 的 `asked_engines` fixture:chat 的端点解析已搬到 chat_invoke,两处都要打桩(embeddings 仍在 openai_compat)。把 `monkeypatch.setattr(openai_compat, "get_vllm_base_url", _fake)` 改为:

```python
    from src.api import chat_invoke
    monkeypatch.setattr(openai_compat, "get_vllm_base_url", _fake)
    monkeypatch.setattr(chat_invoke, "get_vllm_base_url", _fake)
```

- [ ] **Step 6: 跑新测试 + chat 全部回归**

Run:
```bash
uv run pytest tests/test_chat_invoke.py tests/test_thinking_mapping.py tests/test_chat_completions_dispatch.py \
  tests/test_compat_routes_vllm_regression.py tests/test_data_plane_readonly.py tests/test_playground_admin_session_llm.py \
  tests/test_ollama_compat.py tests/test_anthropic_compat.py tests/test_embeddings_endpoint.py -q -n 4
uv run pytest tests -q -n 8 -k "chat or openai or responses or quota or usage or thinking or agent" 
```
Expected: 全 PASS。任何失败先看是不是本次搬家的行为差异 —— **改实现,不改行为断言**。

- [ ] **Step 7: Commit**

```bash
git add backend/src/api/chat_invoke.py backend/src/api/routes/openai_compat.py \
  backend/tests/test_chat_invoke.py backend/tests/test_playground_admin_session_llm.py
git commit -m "refactor(api): 抽出非流式 chat 共用核心 chat_invoke —— 行为不变"
```

---

### Task 3: 抽 `service_access.py` + `prediction_submit.py`(行为不变的重构)

**Files:**
- Create: `backend/src/api/service_access.py`
- Create: `backend/src/api/prediction_submit.py`
- Modify: `backend/src/api/routes/predictions.py`(`_auth_predictions` 约 42-76 行、`_SYNC_CAP_SECONDS`/`_parse_prefer` 约 78-101 行、`_resolve_service` 约 104-135 行、`create_prediction` 约 174-270 行)
- Modify: `backend/tests/test_prediction_service_pr2.py`(`test_route_wired_and_run_deleted`)

**Interfaces:**
- Produces(`src.api.service_access`):
  - `Auth = tuple[ServiceInstance | None, InstanceApiKey | None]`(类型别名,Task 4/5 用)
  - `async auth_bearer_or_admin_session(request: Request, authorization: str | None = Header(default=None), session: AsyncSession = Depends(get_async_session)) -> tuple[ServiceInstance | None, InstanceApiKey | None]`(原 `_auth_predictions`,FastAPI 依赖)
  - `async resolve_service_for_call(session: AsyncSession, auth: tuple[ServiceInstance | None, InstanceApiKey | None], name: str) -> tuple[ServiceInstance, InstanceApiKey | None]`(原 `_resolve_service`)
- Produces(`src.api.prediction_submit`):
  - `parse_prefer(prefer: str | None) -> tuple[bool, float | None]`
  - `check_submittable(instance: ServiceInstance) -> None`(model → 400;非 workflow/comfy_template → 400)
  - `@dataclass(frozen=True) SubmitResult(status_code: int, prediction: dict)`
  - `async submit_prediction(session: AsyncSession, *, app_state: Any, instance: ServiceInstance, api_key: InstanceApiKey | None, inputs: dict, prefer: str | None, webhook: str | None = None, webhook_events_filter: list[str] | None = None) -> SubmitResult`
- `predictions.py` 保留别名 `_parse_prefer = parse_prefer`(`tests/test_prediction_service_pr2.py` 在 import)。

- [ ] **Step 1: 先改源码位置断言(让它按新位置变红)** —— `tests/test_prediction_service_pr2.py::test_route_wired_and_run_deleted`:把

```python
    assert "apply_inputs_to_snapshot(" in pred
    assert "validate_service_input(" in pred  # 接进 PR-1 校验
```

改为

```python
    # 提交核心 2026-09-26 搬到 src/api/prediction_submit.py(与 /v1/skill-runs/generate 共用)。
    assert "submit_prediction(" in pred
    submit = (_SRC / "api/prediction_submit.py").read_text()
    assert "apply_inputs_to_snapshot(" in submit
    assert "validate_service_input(" in submit  # 接进 PR-1 校验
```

- [ ] **Step 2: 跑,确认失败**

Run: `uv run pytest tests/test_prediction_service_pr2.py::test_route_wired_and_run_deleted -q`
Expected: FAIL(`submit_prediction(` 不在 predictions.py)

- [ ] **Step 3: 建 `backend/src/api/service_access.py`**

```python
"""数据面「鉴权 + 按名解析服务」的共用实现(spec 2026-09-26 skill-runs §4)。

`/v1/services/{name}/predictions` 与 `/v1/skill-runs/*` 共用,新路由不 import 别的路由模块的私有函数。
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_auth import enforce_instance_rate_limit, verify_bearer_token_any
from src.models.database import get_async_session
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance
from src.services.model_resolver import ModelNotFound, resolve_target_service

Auth = tuple[ServiceInstance | None, InstanceApiKey | None]


async def auth_bearer_or_admin_session(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> Auth:
    # ← 原 predictions._auth_predictions 的 docstring + 函数体原样粘贴


async def resolve_service_for_call(
    session: AsyncSession, auth: Auth, name: str,
) -> tuple[ServiceInstance, InstanceApiKey | None]:
    # ← 原 predictions._resolve_service 的 docstring + 函数体原样粘贴
```

(占位注释处必须是剪切过来的真实代码。)

- [ ] **Step 4: 建 `backend/src/api/prediction_submit.py`**

```python
"""提交已发布工作流服务的一次 prediction(spec 2026-09-26 skill-runs §4)。

`/v1/services/{name}/predictions` 与 `/v1/skill-runs/generate` 共用:schema 校验 → 动态
enum → 注入快照 → ExecutionTask → 同步/异步等待 → Prediction 对象,只此一份实现。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.execution_task import ExecutionTask
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance
from src.services.comfy.style_options import resolve_dynamic_enums
from src.services.prediction_service import (
    apply_inputs_to_snapshot,
    snapshot_to_executor_form,
    task_to_prediction,
)
from src.services.service_schema import build_service_io_schema, validate_service_input

# 同步默认上限(秒):无 Prefer 时阻塞,但封顶避免无限挂(长任务用 respond-async)。
_SYNC_CAP_SECONDS = 600.0
_SUBMITTABLE_SOURCE_TYPES = ("workflow", "comfy_template")


def parse_prefer(prefer: str | None) -> tuple[bool, float | None]:
    # ← 原 predictions._parse_prefer 的 docstring + 函数体原样粘贴


def check_submittable(instance: ServiceInstance) -> None:
    """只有工作流类服务能提交 prediction;model(LLM)走 chat。"""
    if instance.source_type == "model":
        raise HTTPException(400, detail="model(LLM)服务请用 /v1/chat/completions")
    if instance.source_type not in _SUBMITTABLE_SOURCE_TYPES:
        raise HTTPException(
            400, detail=f"source_type {instance.source_type!r} 暂不支持 predictions")


@dataclass(frozen=True)
class SubmitResult:
    status_code: int
    prediction: dict[str, Any]


async def submit_prediction(
    session: AsyncSession,
    *,
    app_state: Any,
    instance: ServiceInstance,
    api_key: InstanceApiKey | None,
    inputs: dict[str, Any],
    prefer: str | None,
    webhook: str | None = None,
    webhook_events_filter: list[str] | None = None,
) -> SubmitResult:
    """跑一个已发布 workflow 服务(调用方已完成鉴权与服务解析)。"""
    check_submittable(instance)
    # ↓ 从原 create_prediction 剪切:`snapshot = instance.workflow_snapshot or {}` 一直到
    #   `await session.refresh(task)`(sync 分支末尾),**注释全部原样保留**,并做以下替换:
    #   - `inputs = body.input or {}` 这一行删除(inputs 已是参数)
    #   - `webhook_url=body.webhook` → `webhook_url=webhook`
    #   - `webhook_events=body.webhook_events_filter` → `webhook_events=webhook_events_filter`
    #   - `getattr(request.app.state, ...)` → `getattr(app_state, ...)`
    #   - `async_mode, wait_seconds = _parse_prefer(prefer)` → `parse_prefer(prefer)`
    #   - `response.status_code = 202` → `status_code = 202`,并在 if 前加 `status_code = 200`
    #   - 末尾 `return task_to_prediction(...)` 改为:
    #       return SubmitResult(
    #           status_code=status_code,
    #           prediction=task_to_prediction(task, service=instance.name, input_values=inputs))
```

(同上,提交物里是真实代码,没有 `↓` 说明注释。)

- [ ] **Step 5: 改 `predictions.py`**

删掉 `_auth_predictions`、`_SYNC_CAP_SECONDS`、`_parse_prefer`、`_resolve_service` 与 `create_prediction` 的主体,并清掉因此未用的 import(`ruff check` 为准)。import 区加:

```python
from src.api.prediction_submit import parse_prefer, submit_prediction
from src.api.service_access import (
    auth_bearer_or_admin_session as _auth_predictions,
    resolve_service_for_call as _resolve_service,
)

_parse_prefer = parse_prefer  # 旧 import 路径兼容(tests/test_prediction_service_pr2.py)
```

`create_prediction` 变为(docstring 原样保留):

```python
@router.post("/services/{name}/predictions")
async def create_prediction(
    name: str,
    body: PredictionRequest,
    request: Request,
    response: Response,
    prefer: str | None = Header(default=None),
    auth: tuple[ServiceInstance | None, InstanceApiKey | None] = Depends(_auth_predictions),
    session: AsyncSession = Depends(get_async_session),
):
    """(原 docstring 原样保留)"""
    instance, api_key = await _resolve_service(session, auth, name)
    result = await submit_prediction(
        session, app_state=request.app.state, instance=instance, api_key=api_key,
        inputs=body.input or {}, prefer=prefer,
        webhook=body.webhook, webhook_events_filter=body.webhook_events_filter,
    )
    response.status_code = result.status_code
    return result.prediction
```

`get_prediction` / `cancel_prediction` 仍用 `Depends(_auth_predictions)`,不变。

- [ ] **Step 6: 跑 predictions 全部回归**

Run:
```bash
uv run ruff check src/api/prediction_submit.py src/api/service_access.py src/api/routes/predictions.py
uv run pytest tests/test_prediction_service_pr2.py tests/test_run_async_contract.py tests/test_service_schema_pr1.py \
  tests/comfy tests/test_lane_k_lifespan_wiring.py -q -n 4
```
Expected: ruff 干净;测试全 PASS。

- [ ] **Step 7: Commit**

```bash
git add backend/src/api/service_access.py backend/src/api/prediction_submit.py \
  backend/src/api/routes/predictions.py backend/tests/test_prediction_service_pr2.py
git commit -m "refactor(api): 抽出 prediction 提交与服务解析共用实现 —— 行为不变"
```

---

### Task 4: `/v1/skill-runs/preview` + 路由注册 + 静态守卫

**Files:**
- Create: `backend/src/api/routes/skill_runs.py`
- Modify: `backend/src/api/main.py:22`(import 行)与 router 注册区(`app.include_router(predictions_routes.router)` 附近)
- Modify: `backend/tests/test_data_plane_readonly.py`
- Create: `backend/tests/test_skill_runs_generic.py`
- Test: `backend/tests/test_skill_runs_preview.py`

**Interfaces:**
- Consumes: Task 1 的 `parse_skill` / `build_chat_body` / `extract_final_text`;Task 2 的 `resolve_chat_endpoint` / `clamped_max_tokens` / `inject_thinking` / `preflight_quota` / `invoke_chat_nonstream`;Task 3 的 `auth_bearer_or_admin_session` / `resolve_service_for_call`。
- Produces: `src.api.routes.skill_runs.router`(prefix `/v1/skill-runs`);`PreviewRequest`、`GenerateRequest` Pydantic 模型(Task 5 在同文件加 `/generate`)。

- [ ] **Step 1: 写失败测试** `backend/tests/test_skill_runs_preview.py`

```python
"""POST /v1/skill-runs/preview(spec 2026-09-26 skill-runs §3.1)。

用 conftest 的 api_client:预置 model 服务 "qwen3.5"(引擎已"加载",base_url=test-vllm.invalid)+
一把对它有 grant 的 M:N key。上游 vLLM 用本文件的 fake_vllm 打桩(可配回复)。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from src.models.service_instance import ServiceInstance

SKILL = "---\nname: expand\n---\n\n把用户的想法扩写成一段画面描述,只输出描述本身。\n"


def _reply(content, finish="stop"):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
    }


@pytest.fixture
def fake_vllm(monkeypatch):
    state = {"reply": _reply("  a ginger cat on a windowsill, golden hour  "),
             "status": 200, "bodies": [], "usage": []}
    real_post = httpx.AsyncClient.post

    async def _post(self, url, *args, **kwargs):
        if "test-vllm.invalid" in str(url):
            state["bodies"].append(kwargs.get("json"))
            return httpx.Response(state["status"], json=state["reply"],
                                  request=httpx.Request("POST", str(url)))
        return await real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    async def _record(**kw):
        state["usage"].append(kw)
    import src.services.usage_service as usage_service
    monkeypatch.setattr(usage_service, "record_llm_usage", _record)
    return state


def _req(**over):
    body = {"model": "qwen3.5", "skill": {"content": SKILL}, "input": "黄昏窗台上的猫"}
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_preview_returns_final_text(api_client, bearer_headers, fake_vllm):
    r = await api_client.post("/v1/skill-runs/preview", json=_req(), headers=bearer_headers)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["text"] == "a ginger cat on a windowsill, golden hour"
    assert out["model"] == "qwen3.5"
    assert out["skill"] == {"name": "expand"}
    assert out["finish_reason"] == "stop"
    assert out["usage"] == {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}

    sent = fake_vllm["bodies"][0]
    assert sent["messages"][0] == {
        "role": "system", "content": "把用户的想法扩写成一段画面描述,只输出描述本身。"}
    assert sent["messages"][1] == {"role": "user", "content": "黄昏窗台上的猫"}
    assert sent["model"] == ""
    assert "thinking" not in sent                              # 已翻译成 chat_template_kwargs
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}   # 默认 disabled(qwen 白名单)
    assert len(fake_vllm["usage"]) == 1                        # 按普通 chat 记用量


@pytest.mark.asyncio
async def test_preview_content_parts_with_image(api_client, bearer_headers, fake_vllm):
    parts = [{"type": "text", "text": "把背景换成雪夜"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]
    r = await api_client.post("/v1/skill-runs/preview", json=_req(input=parts), headers=bearer_headers)
    assert r.status_code == 200, r.text
    assert fake_vllm["bodies"][0]["messages"][1]["content"] == parts


@pytest.mark.asyncio
async def test_preview_rejects_private_image_url(api_client, bearer_headers, fake_vllm):
    parts = [{"type": "image_url", "image_url": {"url": "http://169.254.169.254/x.png"}}]
    r = await api_client.post("/v1/skill-runs/preview", json=_req(input=parts), headers=bearer_headers)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "unsafe_image_url"
    assert fake_vllm["bodies"] == []


@pytest.mark.asyncio
async def test_preview_admin_session_without_bearer(api_client, fake_vllm):
    r = await api_client.post("/v1/skill-runs/preview", json=_req())
    assert r.status_code == 200, r.text
    assert fake_vllm["usage"][0]["api_key_id"] is None


@pytest.mark.asyncio
async def test_preview_cold_model_is_503_and_never_loads(api_client, bearer_headers, fake_vllm):
    mgr = api_client.app.state.model_manager
    mgr.get_adapter = MagicMock(return_value=None)
    r = await api_client.post("/v1/skill-runs/preview", json=_req(), headers=bearer_headers)
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "model_not_ready"
    mgr.load_model.assert_not_called()
    assert fake_vllm["bodies"] == []


@pytest.mark.asyncio
async def test_preview_unknown_or_ungranted_model_is_404(api_client, bearer_headers, fake_vllm):
    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        s.add(ServiceInstance(source_type="model", source_name="other", name="other-llm",
                              type="llm", status="active"))
        await s.commit()
    for name in ("nope", "other-llm"):          # 不存在 / 存在但这把 key 没 grant
        r = await api_client.post("/v1/skill-runs/preview", json=_req(model=name),
                                  headers=bearer_headers)
        assert r.status_code == 404, (name, r.text)


@pytest.mark.asyncio
async def test_preview_non_model_service_is_400(api_client, fake_vllm):
    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        s.add(ServiceInstance(source_type="workflow", source_id=1, name="some-flow",
                              type="inference", status="active"))
        await s.commit()
    r = await api_client.post("/v1/skill-runs/preview", json=_req(model="some-flow"))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "not_a_chat_model"


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "code"), [
    (_reply("half a prom", finish="length"), "skill_output_truncated"),
    (_reply("   "), "skill_empty_output"),
])
async def test_preview_unusable_output_is_502(api_client, bearer_headers, fake_vllm, reply, code):
    fake_vllm["reply"] = reply
    r = await api_client.post("/v1/skill-runs/preview", json=_req(), headers=bearer_headers)
    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == code


@pytest.mark.asyncio
async def test_preview_upstream_error_passthrough(api_client, bearer_headers, fake_vllm):
    fake_vllm["status"] = 400
    fake_vllm["reply"] = {"error": {"message": "bad image"}}
    r = await api_client.post("/v1/skill-runs/preview", json=_req(), headers=bearer_headers)
    assert r.status_code == 400
    assert r.json() == {"error": {"message": "bad image"}}
    assert fake_vllm["usage"] == []                      # 失败不记账


@pytest.mark.asyncio
@pytest.mark.parametrize(("over", "status", "code"), [
    ({"skill": {"content": "---\nname: x\n---\n  "}}, 422, "skill_empty"),
    ({"input": ""}, 422, "empty_input"),
    ({"options": {"thinking": "maybe"}}, 400, "validation_error"),
    ({"options": {"max_tokens": 0}}, 400, "validation_error"),
])
async def test_preview_request_validation(api_client, bearer_headers, fake_vllm, over, status, code):
    r = await api_client.post("/v1/skill-runs/preview", json=_req(**over), headers=bearer_headers)
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code
    assert fake_vllm["bodies"] == []


@pytest.mark.asyncio
async def test_preview_clamps_max_tokens(api_client, bearer_headers, fake_vllm):
    r = await api_client.post("/v1/skill-runs/preview",
                              json=_req(options={"max_tokens": 100000}), headers=bearer_headers)
    assert r.status_code == 200, r.text
    assert fake_vllm["bodies"][0]["max_tokens"] == 3584   # api_client 的 max_model_len=4096
```

`backend/tests/test_skill_runs_generic.py`:

```python
"""架构守卫(spec 2026-09-26 skill-runs §1 硬约束 1):编排代码不认识任何具体服务/节点。"""
from __future__ import annotations

from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_GENERIC = ("services/skill_run.py", "api/routes/skill_runs.py")
_FORBIDDEN = ("qwen", "comfy", "node_id")


@pytest.mark.parametrize("rel", _GENERIC)
def test_orchestration_code_is_service_agnostic(rel):
    text = (_SRC / rel).read_text(encoding="utf-8").lower()
    for word in _FORBIDDEN:
        assert word not in text, f"{rel} 出现 {word!r} —— 服务/节点细节属于模板数据,不进编排代码"
```

`backend/tests/test_data_plane_readonly.py` —— 在 `test_ensure_vllm_base_url_is_gone` 之后追加:

```python
# spec 2026-09-26 skill-runs:preview 走共用的 chat_invoke,同样对放置只读。
SKILL_RUNS_DATA_PLANE = ("src.api.chat_invoke", "src.api.routes.skill_runs")


def test_skill_runs_path_has_no_load_capability():
    import importlib
    for name in SKILL_RUNS_DATA_PLANE:
        src = inspect.getsource(importlib.import_module(name))
        for bad in ("ensure_vllm_base_url", ".load_model(", ".get_loaded_adapter(", "get_or_load"):
            assert bad not in src, f"{name} 引用 {bad} —— 数据面不得改变放置"
    assert "get_vllm_base_url" in inspect.getsource(importlib.import_module("src.api.chat_invoke"))
```

- [ ] **Step 2: 跑,确认失败**

Run: `uv run pytest tests/test_skill_runs_preview.py tests/test_skill_runs_generic.py tests/test_data_plane_readonly.py -q`
Expected: preview 用例 404(路由不存在);generic / data-plane 新用例 `FileNotFoundError` / `ModuleNotFoundError`

- [ ] **Step 3: 建 `backend/src/api/routes/skill_runs.py`**

```python
"""Skill 驱动的工作流编排(spec 2026-09-26-skill-runs-orchestration-design)。

- POST /v1/skill-runs/preview:用指定 Chat 模型执行调用方内联的 SKILL.md,返回最终文本。
- POST /v1/skill-runs/generate:把最终文本写进调用方指定的 prompt_field,提交任意已发布工作流。

本模块只认「服务名 + 输入 key」,不认识任何具体模型、工作流或其内部结构 —— 那些是模板
管理数据。数据面:对模型放置只读(未加载即 503,绝不加载)。
"""
from __future__ import annotations

from typing import Any, Literal

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
from src.api.service_access import Auth, auth_bearer_or_admin_session, resolve_service_for_call
from src.errors import InvalidRequestError
from src.models.database import get_async_session
from src.services.skill_run import build_chat_body, extract_final_text, parse_skill
from src.utils.url_security import UnsafeURLError, validate_chat_image_urls

router = APIRouter(prefix="/v1/skill-runs", tags=["skill-runs"])

_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


class SkillSpec(BaseModel):
    content: str = Field(..., max_length=200_000)


class PreviewOptions(BaseModel):
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    # 默认关思考:思考 token 计入 max_tokens,写 prompt 类 Skill 开思考极易截断。
    thinking: Literal["enabled", "disabled", "auto"] = "disabled"


class PreviewRequest(BaseModel):
    model: str
    skill: SkillSpec
    input: str | list[dict[str, Any]]
    options: PreviewOptions = Field(default_factory=PreviewOptions)


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
        await validate_chat_image_urls(chat_body["messages"])
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
```

注意:`inject_thinking` 对 `chat_body` 原地 pop/写入 —— `chat_body` 是本函数刚新建的 dict,不是入参,不违反不可变约束。`service_access.Auth` 是 Task 3 定义的类型别名。

- [ ] **Step 4: 注册 router** —— `backend/src/api/main.py` 第 22 行 import 列表末尾追加 `, skill_runs as skill_runs_routes`;在 `app.include_router(predictions_routes.router)` 下一行加 `app.include_router(skill_runs_routes.router)`。

- [ ] **Step 5: 跑,确认通过**

Run: `uv run pytest tests/test_skill_runs_preview.py tests/test_skill_runs_generic.py tests/test_data_plane_readonly.py -q`
Expected: 全 PASS。若 `test_preview_admin_session_without_bearer` 里 `usage[0]["api_key_id"]` 不是 None,说明 admin 旁路没走通,查 `auth_bearer_or_admin_session`。

- [ ] **Step 6: Commit**

```bash
git add backend/src/api/routes/skill_runs.py backend/src/api/main.py backend/tests/test_skill_runs_preview.py \
  backend/tests/test_skill_runs_generic.py backend/tests/test_data_plane_readonly.py
git commit -m "feat(skill-runs): POST /v1/skill-runs/preview —— Chat 模型执行内联 Skill 返回最终文本"
```

---

### Task 5: `/v1/skill-runs/generate`

**Files:**
- Modify: `backend/src/api/routes/skill_runs.py`
- Test: `backend/tests/test_skill_runs_generate.py`

**Interfaces:**
- Consumes: Task 1 `text_input_fields` / `merge_prompt_input`;Task 3 `check_submittable` / `submit_prediction` / `resolve_service_for_call`;`src.services.service_schema.build_service_io_schema(exposed_inputs, exposed_outputs, snapshot) -> {"input_schema", "output_schema"}`。
- Produces: `POST /v1/skill-runs/generate` → 与 predictions 相同的 Prediction dict。

- [ ] **Step 1: 写失败测试** `backend/tests/test_skill_runs_generate.py`

```python
"""POST /v1/skill-runs/generate(spec 2026-09-26 skill-runs §3.2)。

刻意用一个与任何真实模板无关的**通用假工作流服务**,文本字段叫 `caption` 而不是 `prompt`
—— 证明编排只认「服务名 + 输入 key」。执行器打桩,只记录收到的快照。
"""
from __future__ import annotations

import bcrypt
import pytest

from src.models.api_gateway import ApiKeyGrant
from src.models.execution_task import ExecutionTask
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance

SNAPSHOT = {"nodes": {
    "n1": {"class_type": "FakeText", "inputs": {"text": ""}},
    "n2": {"class_type": "FakeLoader", "inputs": {"file": ""}},
    "n3": {"class_type": "FakeSampler", "inputs": {"steps": 20, "style": "a"}},
}}
EXPOSED = [
    {"key": "caption", "node_id": "n1", "input_name": "text", "type": "string", "required": True},
    {"key": "ref", "node_id": "n2", "input_name": "file", "type": "image", "required": False},
    {"key": "style", "node_id": "n3", "input_name": "style", "type": "string", "required": False,
     "constraints": {"enum": ["a", "b"]}},
    {"key": "steps", "node_id": "n3", "input_name": "steps", "type": "integer", "required": False},
]


@pytest.fixture
def captured_runs(monkeypatch):
    runs: list[dict] = []

    async def _fake_run(task_id, snapshot, **_kw):
        runs.append(snapshot)

    import src.services.workflow_runner as workflow_runner
    monkeypatch.setattr(workflow_runner, "run_workflow_task", _fake_run)
    return runs


async def _service(db_session, name="any-captioner", source_type="workflow"):
    svc = ServiceInstance(
        name=name, type="inference", status="active", source_type=source_type,
        source_id=1, source_name=name if source_type == "model" else None,
        category="video", workflow_snapshot=SNAPSHOT, exposed_inputs=EXPOSED, exposed_outputs=[],
    )
    db_session.add(svc)
    await db_session.commit()
    await db_session.refresh(svc)
    return svc


def _req(**over):
    body = {"service": "any-captioner", "prompt_field": "caption",
            "text": "a slow dolly-in on a lighthouse at dusk", "input": {"steps": 8}}
    body.update(over)
    return body


def _node(snapshot: dict, node_id: str) -> dict:
    return next(n for n in snapshot["nodes"] if n["id"] == node_id)


@pytest.mark.asyncio
async def test_generate_injects_text_into_prompt_field(db_client, db_session, captured_runs):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate", json=_req())
    assert r.status_code == 200, r.text
    pred = r.json()
    assert pred["service"] == "any-captioner"
    assert pred["input"] == {"steps": 8, "caption": "a slow dolly-in on a lighthouse at dusk"}
    snap = captured_runs[0]
    assert _node(snap, "n1")["data"]["text"] == "a slow dolly-in on a lighthouse at dusk"
    assert _node(snap, "n3")["data"]["steps"] == 8
    task = await db_session.get(ExecutionTask, int(pred["id"]))
    assert task.input_json["caption"] == "a slow dolly-in on a lighthouse at dusk"


@pytest.mark.asyncio
async def test_generate_respond_async_is_202(db_client, db_session, captured_runs):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate", json=_req(),
                             headers={"Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] in ("starting", "processing")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["ref", "style", "steps", "nope"])
async def test_generate_rejects_non_text_prompt_field(db_client, db_session, captured_runs, field):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate", json=_req(prompt_field=field))
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "invalid_prompt_field"
    assert err["text_fields"] == ["caption"]
    assert captured_runs == []


@pytest.mark.asyncio
async def test_generate_prompt_field_conflict(db_client, db_session, captured_runs):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate",
                             json=_req(input={"caption": "from input"}))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "prompt_field_conflict"


@pytest.mark.asyncio
async def test_generate_empty_text(db_client, db_session, captured_runs):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate", json=_req(text="  "))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "empty_text"


@pytest.mark.asyncio
async def test_generate_other_inputs_still_schema_validated(db_client, db_session, captured_runs):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate", json=_req(input={"steps": "many"}))
    assert r.status_code == 422, r.text          # predictions 既有的 schema 校验照常生效
    assert captured_runs == []


@pytest.mark.asyncio
async def test_generate_on_model_service_is_400(db_client, db_session, captured_runs):
    await _service(db_session, name="some-llm", source_type="model")
    r = await db_client.post("/v1/skill-runs/generate", json=_req(service="some-llm"))
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_generate_unknown_service_is_404(db_client, db_session, captured_runs):
    r = await db_client.post("/v1/skill-runs/generate", json=_req(service="nope"))
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_generate_bearer_key_needs_grant(db_client, db_session, captured_runs):
    svc = await _service(db_session)
    await _service(db_session, name="not-granted")
    raw = "sk-gen1234abcdef00"
    key = InstanceApiKey(instance_id=None, label="t", is_active=True, key_prefix=raw[:10],
                         key_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode())
    db_session.add(key)
    await db_session.commit()
    await db_session.refresh(key)
    db_session.add(ApiKeyGrant(api_key_id=key.id, service_id=svc.id, status="active"))
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}

    ok = await db_client.post("/v1/skill-runs/generate", json=_req(), headers=headers)
    assert ok.status_code == 200, ok.text
    task = await db_session.get(ExecutionTask, int(ok.json()["id"]))
    assert task.api_key_id == key.id              # 归属到 key(IDOR 防护依赖它)

    denied = await db_client.post("/v1/skill-runs/generate",
                                  json=_req(service="not-granted"), headers=headers)
    assert denied.status_code == 404, denied.text
```

- [ ] **Step 2: 跑,确认失败**

Run: `uv run pytest tests/test_skill_runs_generate.py -q`
Expected: 全部 404/405(路由不存在)

- [ ] **Step 3: 实现** —— 在 `skill_runs.py`:

import 区加:

```python
from src.api.prediction_submit import check_submittable, submit_prediction
from src.services.service_schema import build_service_io_schema
```

并把 `from src.services.skill_run import ...` 改为:

```python
from src.services.skill_run import (
    build_chat_body,
    extract_final_text,
    merge_prompt_input,
    parse_skill,
    text_input_fields,
)
```

`PreviewRequest` 之后加模型,文件末尾加端点:

```python
class GenerateRequest(BaseModel):
    service: str
    prompt_field: str
    text: str
    input: dict[str, Any] = Field(default_factory=dict)
    webhook: str | None = None
    webhook_events_filter: list[str] | None = None
```

```python
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
```

- [ ] **Step 4: 跑,确认通过 + 静态守卫**

Run: `uv run pytest tests/test_skill_runs_generate.py tests/test_skill_runs_generic.py -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/api/routes/skill_runs.py backend/tests/test_skill_runs_generate.py
git commit -m "feat(skill-runs): POST /v1/skill-runs/generate —— 文本注入 prompt_field 提交任意工作流"
```

---

### Task 6: 文档 + E2E 素材与脚本 + 全量回归

**Files:**
- Create: `docs/skill-runs.md`
- Modify: `CLAUDE.md`(「GPU 放置」节「数据面对放置只读」那条)
- Create: `backend/tests/manual/skill_runs/qwen-image-t2i.SKILL.md`
- Create: `backend/tests/manual/skill_runs/qwen-image-edit.SKILL.md`
- Create: `backend/tests/manual/verify_skill_runs.py`

**Interfaces:**
- Consumes: 已实现的两个端点;生产服务 `qwen21-text-to-image`(字段 prompt/aspect_ratio/megapixels/seed)、`qwen21-image-edit`(另加 image);Chat 服务 `qwen3-8-27b`。

- [ ] **Step 1: 写 `backend/tests/manual/skill_runs/qwen-image-t2i.SKILL.md`**

```markdown
---
name: qwen-image-t2i
description: 把用户的简短想法扩写成 Qwen Image 文生图提示词
---

你是文生图提示词撰写者。把用户给出的想法扩写成**一段**可直接用于图像生成的中文提示词。

要求:
- 写清主体、动作/姿态、环境与背景、光线与时间、色调、构图与镜头(景别、视角)、画面风格与质感。
- 只描述画面中看得见的内容,不写情绪评价,不写"一张图片展示了"之类的套话。
- 用户明确给出的要素必须保留,不得改写其含义;用户没说的细节合理补全。
- 画面里若要出现文字,用中文引号把文字原样写出。
- 长度 80–200 字。

只输出提示词本身:不要标题、不要解释、不要引号包裹、不要列表。
```

- [ ] **Step 2: 写 `backend/tests/manual/skill_runs/qwen-image-edit.SKILL.md`**

```markdown
---
name: qwen-image-edit
description: 看原图,把用户的修改意图写成 Qwen Image 图像编辑指令
---

你是图像编辑指令撰写者。用户会给你一张原图和一句修改意图。先看清原图里的主体、布局、光线与风格,
再把修改意图写成**一条**精确的中文编辑指令。

要求:
- 指明要改什么、改成什么样,位置/范围说具体(例如"背景"、"人物左手里的杯子")。
- 明确要求保持不变的部分:主体身份与外貌、姿态、构图、画风,除非用户要求改变它们。
- 新增或改变的元素要与原图光线、透视、风格一致。
- 长度 30–120 字。

只输出编辑指令本身:不要复述原图、不要解释、不要引号包裹。
```

- [ ] **Step 3: 写 `backend/tests/manual/verify_skill_runs.py`**

```python
"""skill-runs 真机端到端(非 CI):Qwen Image 2.1 文生图 + 图像编辑。

前提:含 /v1/skill-runs 的后端已上线;Chat 模型已加载;ComfyUI 在线且没有渲染在跑。
鉴权:backend/.env 的 ADMIN_TOKEN(bearer 旁路)。

    cd backend && uv run python tests/manual/verify_skill_runs.py \
        [--base http://127.0.0.1:8000] [--model qwen3-8-27b] [--out ./skill_runs_out]

流程:preview(t2i Skill)→ generate qwen21-text-to-image → 下载图;
      preview(edit Skill + 上一步的图)→ generate qwen21-image-edit(input.image=同一张图)→ 下载图。
"""
from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE.parents[1] / ".env"
POLL_INTERVAL_S = 3.0
POLL_TIMEOUT_S = 15 * 60
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _admin_token() -> str:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    sys.exit(f"{ENV_FILE} 里没有 ADMIN_TOKEN")


def _check(r: httpx.Response, what: str) -> dict[str, Any]:
    if r.status_code >= 400:
        sys.exit(f"[FAIL] {what}: HTTP {r.status_code} {r.text[:800]}")
    return r.json()


def _image_urls(output: Any) -> list[str]:
    """递归找产物里的图片 URL(桥节点产物是 {"items":[{"url","kind","filename"}]})。"""
    found: list[str] = []
    if isinstance(output, dict):
        url = output.get("url")
        name = str(output.get("filename") or url or "").lower()
        if isinstance(url, str) and (output.get("kind") == "image" or name.endswith(IMAGE_EXTS)):
            found.append(url)
        for v in output.values():
            found.extend(_image_urls(v))
    elif isinstance(output, list):
        for v in output:
            found.extend(_image_urls(v))
    return list(dict.fromkeys(found))


def _preview(c: httpx.Client, model: str, skill: str, user_input: Any) -> str:
    t0 = time.monotonic()
    out = _check(c.post("/v1/skill-runs/preview", json={
        "model": model,
        "skill": {"content": (HERE / "skill_runs" / skill).read_text(encoding="utf-8")},
        "input": user_input,
    }), f"preview {skill}")
    print(f"[preview] {skill} {time.monotonic() - t0:.1f}s usage={out['usage']}\n  → {out['text']}")
    return out["text"]


def _generate(c: httpx.Client, service: str, text: str, extra: dict[str, Any]) -> dict[str, Any]:
    pred = _check(c.post("/v1/skill-runs/generate", headers={"Prefer": "respond-async"}, json={
        "service": service, "prompt_field": "prompt", "text": text, "input": extra,
    }), f"generate {service}")
    pid, t0 = pred["id"], time.monotonic()
    print(f"[generate] {service} prediction={pid} status={pred['status']}")
    while pred["status"] not in ("succeeded", "failed", "canceled"):
        if time.monotonic() - t0 > POLL_TIMEOUT_S:
            sys.exit(f"[FAIL] {service} prediction {pid} 超时")
        time.sleep(POLL_INTERVAL_S)
        pred = _check(c.get(f"/v1/predictions/{pid}"), f"poll {pid}")
    if pred["status"] != "succeeded":
        sys.exit(f"[FAIL] {service} prediction {pid}: {pred['status']} {pred.get('error')}")
    print(f"[generate] {service} succeeded in {time.monotonic() - t0:.0f}s")
    return pred


def _download_first_image(c: httpx.Client, base: str, pred: dict[str, Any], dest: Path) -> Path:
    urls = _image_urls(pred.get("output"))
    if not urls:
        sys.exit(f"[FAIL] prediction {pred['id']} 没有图片产物: {pred.get('output')}")
    r = c.get(urljoin(base, urls[0]))
    if r.status_code != 200 or not r.content:
        sys.exit(f"[FAIL] 下载 {urls[0]}: HTTP {r.status_code}")
    dest.write_bytes(r.content)
    print(f"[saved] {dest} ({len(r.content)} bytes)")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--model", default="qwen3-8-27b")
    ap.add_argument("--out", default="./skill_runs_out")
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # trust_env=False:本机 ALL_PROXY=socks5(mihomo)会让 httpx 走代理且缺 socksio。
    with httpx.Client(base_url=args.base, trust_env=False, timeout=600,
                      headers={"Authorization": f"Bearer {_admin_token()}"}) as c:
        t2i_text = _preview(c, args.model, "qwen-image-t2i.SKILL.md",
                            "一只橘猫趴在黄昏的木窗台上,窗外是老城区的屋顶")
        t2i = _generate(c, "qwen21-text-to-image", t2i_text, {"aspect_ratio": "1:1 (Perfect Square)"})
        src_img = _download_first_image(c, args.base, t2i, out_dir / "t2i.png")

        data_url = "data:image/png;base64," + base64.b64encode(src_img.read_bytes()).decode()
        edit_text = _preview(c, args.model, "qwen-image-edit.SKILL.md", [
            {"type": "text", "text": "把窗外换成下雪的夜晚,猫保持不变"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ])
        edit = _generate(c, "qwen21-image-edit", edit_text, {"image": data_url})
        _download_first_image(c, args.base, edit, out_dir / "edit.png")
    print("[OK] skill-runs 端到端通过 —— 请目检", out_dir.resolve())


if __name__ == "__main__":
    main()
```

执行者注意:`aspect_ratio` 的合法值以 `GET /v1/services/qwen21-text-to-image/schema` 为准;若 `"1:1 (Perfect Square)"` 不在 enum 里,改用 schema 里的某个 1:1 值,或干脆不传(用模板默认)。这一处属于测试素材,不影响编排代码。

- [ ] **Step 4: 写 `docs/skill-runs.md`**

```markdown
# skill-runs:Skill 驱动的工作流编排

把「用 Chat 模型执行一段 Skill → 得到一段文本 → 注入任意已发布工作流的文本字段 → 提交」标准化。
设计见 `docs/superpowers/specs/2026-09-26-skill-runs-orchestration-design.md`。

## 两步流程

1. `POST /v1/skill-runs/preview` —— 拿到最终文本(可展示给用户修改)。
2. `POST /v1/skill-runs/generate` —— 把文本写进工作流的 `prompt_field` 提交,返回 Prediction;
   用 `GET /v1/predictions/{id}` 轮询(或 `/stream`、`/cancel`)。

鉴权与 `/v1/chat/completions`、`/v1/services/{name}/predictions` 相同:M:N key 需对 Chat 服务
(preview)与工作流服务(generate)**分别**有 grant。

## preview

    curl -s http://<engine>:8000/v1/skill-runs/preview \
      -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{
        "model": "qwen3-8-27b",
        "skill": {"content": "---\nname: my-skill\n---\n把想法扩写成画面描述,只输出描述。"},
        "input": "黄昏窗台上的猫",
        "options": {"max_tokens": 1024, "thinking": "disabled"}
      }'
    → {"text": "...", "model": "qwen3-8-27b", "skill": {"name": "my-skill"},
       "usage": {...}, "finish_reason": "stop"}

- `skill.content`:完整 SKILL.md。frontmatter 可选(只读 `name`);正文作为 system 指令。
- `input`:字符串,或 OpenAI content parts(`text` + `image_url`,支持 `data:` 与公网 https),
  给视觉模型看图用(如图像编辑类 Skill)。
- `options.thinking` 默认 `disabled`(思考 token 会占 `max_tokens`,容易截断)。

## generate

    curl -s http://<engine>:8000/v1/skill-runs/generate \
      -H "Authorization: Bearer $KEY" -H 'Prefer: respond-async' -H 'Content-Type: application/json' -d '{
        "service": "qwen21-image-edit",
        "prompt_field": "prompt",
        "text": "<preview 返回的 text>",
        "input": {"image": "data:image/png;base64,..."}
      }'
    → 202 + Prediction(与 predictions 完全相同)

- `prompt_field` 必须是该服务的**自由文本**输入(string、非文件、无选项清单);
  可选值见 `GET /v1/services/{service}/schema`,选错时错误体 `error.text_fields` 列出可用字段。
- `input` 里不能再带 `prompt_field`(422 `prompt_field_conflict`)。其余字段照 schema 校验。
- `Prefer`:缺省同步(封顶 600s)/ `respond-async`(202)/ `wait=N`。

## 错误码

| code | HTTP | 含义 |
|---|---|---|
| `model_not_ready` | 503 | Chat 模型未加载(引擎不会在请求路径上加载;见 `GET /v1/models`) |
| `not_a_chat_model` | 400 | preview 的 `model` 不是 Chat 模型服务 |
| `unsafe_image_url` | 400 | `image_url` 指向私网/回环/非 https |
| `skill_empty` / `skill_invalid_frontmatter` / `empty_input` | 422 | 请求内容不可用 |
| `skill_output_truncated` | 502 | 输出被 `max_tokens` 截断 —— 调大或关思考 |
| `skill_empty_output` | 502 | 模型没有输出文本 |
| `invalid_prompt_field` / `prompt_field_conflict` / `empty_text` | 422 | generate 字段问题 |
| `validation_error` | 400 | 请求体结构/取值不合法 |

## 接入新的工作流(视频 / 音频 / 3D …)

不需要改引擎代码。把工作流发布成服务(画布发布或 ComfyUI 模板 + 映射),确保它暴露一个
string 类型的文本输入;调用方把那个 key 作为 `prompt_field` 即可。节点映射属于模板数据
(`PUT /api/v1/comfy-templates/{id}/mapping`),不属于编排。

## 边界

只支持「Skill 输出一段文本 → 注入一个字段」。结构化输出(同时产出 negative prompt、比例、
镜头参数…)到时另开 spec。
```

- [ ] **Step 5: 改 `CLAUDE.md`** —— 在「GPU 放置」节,把

```
  这里的「数据面」是**五个 LLM 兼容路由模块**:`openai_compat` / `anthropic_compat` /
  `ollama_compat` / `responses` / `context_cache`。
```

改为

```
  这里的「数据面」是**五个 LLM 兼容路由模块**:`openai_compat` / `anthropic_compat` /
  `ollama_compat` / `responses` / `context_cache`,外加 `/v1/skill-runs/*`(它经共用的
  `src/api/chat_invoke.py` 调 chat,同一测试文件静态锁住)。
```

并在「## ComfyUI 桥」节之前新增一节:

```markdown
## skill-runs(Skill 驱动工作流编排)

- `POST /v1/skill-runs/preview`(Chat 模型执行调用方**内联**的 SKILL.md → 最终文本)+
  `POST /v1/skill-runs/generate`(文本写进 `prompt_field` → 提交任意已发布工作流)。
  接入说明 `docs/skill-runs.md`,spec `docs/superpowers/specs/2026-09-26-skill-runs-orchestration-design.md`。
- **编排代码不认识任何具体服务/节点**:`services/skill_run.py` 与 `routes/skill_runs.py` 不得出现
  qwen / comfy / node_id(`tests/test_skill_runs_generic.py` 锁住)。某个模板的节点映射有问题,
  改模板数据(`/api/v1/comfy-templates/{id}/mapping`),别往编排里加特例。
- chat 非流式核心(readiness / 引擎引用 / 用量 / 配额)与 prediction 提交核心各**只有一份**:
  `src/api/chat_invoke.py`、`src/api/prediction_submit.py`;`/v1/chat/completions`、
  `/v1/services/{name}/predictions` 与 skill-runs 共用。改其中行为时三条路由一起受影响。
- 真机验证 `uv run python tests/manual/verify_skill_runs.py`(Qwen Image 2.1 t2i + edit,非 CI)。
```

- [ ] **Step 6: 全量回归 + lint**

Run(先 `nvidia-smi` 确认驱动活着,且确认没有别人在跑全量):
```bash
uv run ruff check src tests/test_skill_run.py tests/test_chat_invoke.py tests/test_skill_runs_*.py tests/manual/verify_skill_runs.py
uv run pytest tests -q -n 8 -p no:cacheprovider \
  --ignore=tests/test_integration_smoke.py --ignore=tests/test_tts_engine_adapter.py
```
Expected: ruff 干净;pytest 全绿(与 CI 同 ignore)。

- [ ] **Step 7: Commit**

```bash
git add docs/skill-runs.md CLAUDE.md backend/tests/manual/skill_runs backend/tests/manual/verify_skill_runs.py
git commit -m "docs(skill-runs): 接入说明、CLAUDE.md 不变式、Qwen Image 2.1 端到端素材与脚本"
```

---

### Task 7: PR → 上线 → 真机端到端(需用户放行)

**Files:** 无代码改动(E2E 若暴露缺陷,回到对应 Task 按 TDD 修)。

- [ ] **Step 1: 推分支、开 PR**

```bash
git push -u origin feat/skill-runs
gh pr create --base master --title "feat: skill-runs —— Skill 驱动的通用工作流编排 API" --body-file - <<'EOF'
## Summary
- `POST /v1/skill-runs/preview`:指定 Chat 模型执行调用方内联的 SKILL.md,返回最终文本(可带图)。
- `POST /v1/skill-runs/generate`:文本写入调用方指定的 `prompt_field`,提交任意已发布工作流,返回 Prediction。
- 重构:chat 非流式核心 → `src/api/chat_invoke.py`;prediction 提交 → `src/api/prediction_submit.py`;鉴权解析 → `src/api/service_access.py`。老路由行为不变。
- 编排代码不含任何具体服务/节点(静态测试锁定);Qwen Image 2.1 映射仍是模板数据。

Spec: docs/superpowers/specs/2026-09-26-skill-runs-orchestration-design.md

## Test plan
- [ ] CI 全绿
- [ ] 上线后 `uv run python tests/manual/verify_skill_runs.py`:t2i + edit 均 succeeded,目检两张图
EOF
```

- [ ] **Step 2: 等 CI 全绿**:`gh pr checks --watch`。红了按 superpowers:systematic-debugging 查,修完回到这里。

- [ ] **Step 3: 停下,请用户放行上线。** 上线 = 用户在 Mac 上 `./infra/ship.sh <PR号>`(或明确授权后由我们执行)。**未获明确同意不得合并/部署。**

- [ ] **Step 4: 上线后跑 E2E**(生产机;先确认 ComfyUI 空闲、Chat 模型 ready):

```bash
cd /media/heygo/program/projects-code/repos/nous-engine/backend
curl -s --noproxy '*' http://127.0.0.1:8000/v1/services/qwen21-text-to-image/schema | python3 -m json.tool | head -40
uv run python /media/heygo/program/projects-code/repos/nous-engine/.claude/worktrees/skill-runs/backend/tests/manual/verify_skill_runs.py \
  --out /tmp/claude-1000/skill_runs_out
```
Expected: 末行 `[OK] skill-runs 端到端通过`;`t2i.png`、`edit.png` 交给用户目检(编辑图应保留猫、窗外变雪夜)。

- [ ] **Step 5: 向用户报告**:两段 preview 文本、两次 generate 耗时、两张图路径;如有失败,附原始错误体。
