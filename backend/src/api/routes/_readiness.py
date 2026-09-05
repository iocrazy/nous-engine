"""服务就绪判定 —— /v1/models 与 model_not_ready 错误体共用,保证两处口径一致。

spec 2026-09-05 §5/§6。只看 ModelManager.is_loaded(不用 get_adapter:它会 touch()
刷新 last_used,列一次模型就把 TTL 续了)。非 model 类服务(comfy_template / workflow /
app)不占 nous-engine 显存,一律视为就绪 —— 它们的就绪由各自执行路径负责。
"""
from __future__ import annotations

from typing import Any, Iterable


def engine_name_of(svc: Any) -> str:
    """服务名 ↔ 引擎名:`source_name or str(source_id)`。

    注意这**不是**唯一实现 —— 六处路由(chat / embeddings / anthropic / ollama /
    responses / context_cache)仍各自内联同一个表达式。本函数是那份内联写法的镜像,
    改口径时六处要一起改(2026-09-05 复审:原注释写"唯一写法"是不实的)。
    """
    return svc.source_name or str(svc.source_id)


def service_is_ready(model_mgr: Any, svc: Any) -> bool:
    if getattr(svc, "source_type", None) != "model":
        return True
    if model_mgr is None:
        return False
    return bool(model_mgr.is_loaded(engine_name_of(svc)))


def ready_model_names(model_mgr: Any, services: Iterable[Any]) -> list[str]:
    """该批服务里「model 类且已加载」的服务名,按传入顺序。"""
    return [s.name for s in services
            if getattr(s, "source_type", None) == "model" and service_is_ready(model_mgr, s)]
