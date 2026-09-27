"""EngineInfo.kind 默认值。

从 test_engine_catalog.py 挪来(2026-09-26 自建图像引擎删除,Task 4):engine_catalog 模块
(超分 / 组件 / LoRA 条目)整删,这条 schema 断言逐字保留。
"""
from __future__ import annotations


def test_engine_info_has_kind_field():
    from src.models.schemas import EngineInfo  # noqa: PLC0415

    e = EngineInfo(name="x", display_name="X", type="image", status="unloaded", gpu=0, vram_gb=1.0, resident=False)
    assert e.kind == "model"  # 默认
