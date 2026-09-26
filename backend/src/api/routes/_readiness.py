"""服务就绪判定 —— /v1/models 与 model_not_ready 错误体共用,保证两处口径一致。

spec 2026-09-05 §5/§6。只看 ModelManager.is_loaded(不用 get_adapter:它会 touch()
刷新 last_used,列一次模型就把 TTL 续了)。非 model 类服务(workflow / app)不占 nous-engine 显存,
一律视为就绪 —— 它们的就绪由各自执行路径负责。

comfy_template(ComfyUI 桥)例外(2026-09-26):它的就绪 = sidecar 在线。此前一律 True,
ComfyUI 停了一天多那阵子 /v1/models 里 5 个桥服务照样「可用」,nous-app 一提交就失败。
探测结果由调用方传入(`comfy_online`),没传则维持旧口径 —— 只有 /v1/models 这类真去
探过的路径收紧,ollama /api/tags 等只关心 model 类的调用方行为不变。
"""
from __future__ import annotations

import time
from typing import Any, Iterable

# sidecar 在线探测的短 TTL 缓存:nous-app 每 30~60s 轮询 /v1/models,每个请求都打
# ComfyUI 的 /queue 没必要。5s 内复用上一次结果。
_COMFY_PROBE_TTL_S = 5.0
_comfy_probe: tuple[float, bool] | None = None   # (monotonic 时刻, 在线)


def reset_comfy_probe_cache() -> None:
    global _comfy_probe
    _comfy_probe = None


async def comfy_sidecar_online() -> bool:
    """ComfyUI sidecar 是否在线(/queue 可达)。走 ComfyClient.health(),trust_env=False。"""
    global _comfy_probe
    now = time.monotonic()
    if _comfy_probe is not None and now - _comfy_probe[0] < _COMFY_PROBE_TTL_S:
        return _comfy_probe[1]
    from src.services.comfy import client as comfy_client  # noqa: PLC0415 — 避免路由层启动期循环
    try:
        online = bool((await comfy_client.get_comfy_client().health()).get("online"))
    except Exception:  # noqa: BLE001 — 探测失败一律按离线,不让 /v1/models 500
        online = False
    _comfy_probe = (now, online)
    return online


def engine_name_of(svc: Any) -> str:
    """服务名 ↔ 引擎名:`source_name or str(source_id)`。

    注意这**不是**唯一实现 —— 六处路由(chat / embeddings / anthropic / ollama /
    responses / context_cache)仍各自内联同一个表达式。本函数是那份内联写法的镜像,
    改口径时六处要一起改(2026-09-05 复审:原注释写"唯一写法"是不实的)。
    """
    return svc.source_name or str(svc.source_id)


def service_is_ready(model_mgr: Any, svc: Any, *, comfy_online: bool | None = None) -> bool:
    st = getattr(svc, "source_type", None)
    if st == "comfy_template":
        return True if comfy_online is None else comfy_online
    if st != "model":
        return True
    if model_mgr is None:
        return False
    return bool(model_mgr.is_loaded(engine_name_of(svc)))


def ready_model_names(model_mgr: Any, services: Iterable[Any]) -> list[str]:
    """该批服务里「model 类且已加载」的服务名,按传入顺序。"""
    return [s.name for s in services
            if getattr(s, "source_type", None) == "model" and service_is_ready(model_mgr, s)]
