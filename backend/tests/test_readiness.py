"""`_readiness` 的单元测试 —— /v1/models、/api/tags 与 503 model_not_ready 共用的就绪口径。

spec 2026-09-05 §5/§6。之前这个模块被三条路由共用却一个直测都没有(2026-09-05 复审):
口径本身改错(比如 model_mgr 缺失时 fail-open)只会在集成测试里以别的形态露出来。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.api.routes._readiness import (
    engine_name_of,
    ready_model_names,
    service_is_ready,
)


def _svc(name: str, *, source_type: str = "model", source_name: str | None = None,
         source_id: int = 7):
    return SimpleNamespace(name=name, source_type=source_type,
                           source_name=source_name, source_id=source_id)


def test_non_model_service_is_always_ready():
    """comfy_template / workflow / app 不占 nous-engine 显存,就绪由各自执行路径负责。"""
    mgr = MagicMock()
    mgr.is_loaded = MagicMock(return_value=False)
    assert service_is_ready(mgr, _svc("krea2", source_type="workflow")) is True
    mgr.is_loaded.assert_not_called()          # 压根不该去问 ModelManager


def test_missing_model_manager_fails_closed():
    """app.state 还没初始化 → 一律不就绪。fail-open 会让 /v1/models 列出一堆冷模型。"""
    assert service_is_ready(None, _svc("a", source_name="engine_a")) is False


def test_loaded_model_service_is_ready():
    mgr = MagicMock()
    mgr.is_loaded = MagicMock(return_value=True)
    assert service_is_ready(mgr, _svc("a", source_name="engine_a")) is True
    mgr.is_loaded.assert_called_once_with("engine_a")


def test_cold_model_service_is_not_ready():
    mgr = MagicMock()
    mgr.is_loaded = MagicMock(return_value=False)
    assert service_is_ready(mgr, _svc("a", source_name="engine_a")) is False


def test_engine_name_falls_back_to_source_id():
    assert engine_name_of(_svc("a", source_name=None, source_id=42)) == "42"


def test_ready_model_names_keeps_input_order_and_drops_non_model():
    """按传入顺序返回,且只含 model 类 —— 顺序是 /v1/models 与错误体里 ready_models 的口径。"""
    mgr = MagicMock()
    mgr.is_loaded = MagicMock(side_effect=lambda n: n in {"e_hot", "e_hot2"})
    services = [
        _svc("hot", source_name="e_hot"),
        _svc("cold", source_name="e_cold"),
        _svc("wf", source_type="workflow", source_name="e_wf"),
        _svc("hot2", source_name="e_hot2"),
    ]
    assert ready_model_names(mgr, services) == ["hot", "hot2"]
