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


@pytest.mark.parametrize(("parts", "hint"), [
    ([{"type": "video_url", "video_url": {"url": "http://example.com/v.mp4"}}], "video_url"),
    ([{"type": "audio_url", "audio_url": {"url": "http://example.com/a.wav"}}], "audio_url"),
    ([{"image_url": "http://example.com/x.png"}], "None"),               # 缺 type
    (["just a string"], "[0]"),                                          # 非 dict
    ([{"type": "image_url", "image_url": "http://example.com/x.png"}], "image_url"),  # 裸字符串
    ([{"type": "image_url", "image_url": {"url": 123}}], "image_url"),   # url 非 str
    ([{"type": "text", "text": "ok"}, {"type": "text", "text": 42}], "[1]"),  # text 非 str
    ([{"type": "text"}], "text"),                                        # 缺 text
])
def test_build_chat_body_rejects_bad_content_part(parts, hint):
    with pytest.raises(UnprocessableError) as ei:
        build_chat_body(_SKILL, parts, temperature=None, max_tokens=None, thinking="auto")
    assert ei.value.code == "invalid_input_part"
    assert ei.value.param == "input"
    assert hint in str(ei.value)


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


@pytest.mark.parametrize("data", [
    {"choices": ["x"]},                                                  # choice 非 dict
    {"choices": [{"message": "hi", "finish_reason": "stop"}]},           # message 非 dict
    {"choices": [{"message": {"content": [{"type": "text", "text": "a"}]},
                  "finish_reason": "stop"}]},                            # content 非 str
])
def test_extract_final_text_malformed_choice_shape_is_502(data):
    with pytest.raises(BadGatewayError) as ei:
        extract_final_text(data)
    assert ei.value.code == "skill_bad_upstream_response"


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


# ---------- 单个 content part 的尺寸上限(2026-09-27 审查延后项) ----------

def test_text_part_too_long_is_422():
    from src.services.skill_run import MAX_TEXT_PART_CHARS
    parts = [{"type": "text", "text": "x" * (MAX_TEXT_PART_CHARS + 1)}]
    with pytest.raises(UnprocessableError) as ei:
        build_chat_body(_SKILL, parts, temperature=None, max_tokens=None, thinking="auto")
    assert ei.value.code == "invalid_input_part"


def test_image_url_too_long_is_422():
    from src.services.skill_run import MAX_IMAGE_URL_CHARS
    url = "data:image/png;base64," + "A" * MAX_IMAGE_URL_CHARS
    parts = [{"type": "image_url", "image_url": {"url": url}}]
    with pytest.raises(UnprocessableError) as ei:
        build_chat_body(_SKILL, parts, temperature=None, max_tokens=None, thinking="auto")
    assert ei.value.code == "invalid_input_part"


def test_parts_at_the_limit_pass():
    from src.services.skill_run import MAX_IMAGE_URL_CHARS, MAX_TEXT_PART_CHARS
    parts = [
        {"type": "text", "text": "x" * MAX_TEXT_PART_CHARS},
        {"type": "image_url", "image_url": {"url": "d" * MAX_IMAGE_URL_CHARS}},
    ]
    body = build_chat_body(_SKILL, parts, temperature=None, max_tokens=None, thinking="auto")
    assert body["messages"][1]["content"] == parts
