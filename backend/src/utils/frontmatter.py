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
