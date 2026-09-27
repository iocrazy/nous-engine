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
