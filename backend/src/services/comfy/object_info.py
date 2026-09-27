"""按节点类型读 ComfyUI object_info(`/object_info/{class}`),带进程内 TTL 缓存。

桥的 `omit_when_empty` 剪枝(`graph_prune.py`)要知道下游节点的某个输入是 required 还是
optional。只取剪枝会碰到的那几个类型,不拉整份 `/object_info`(几 MB、15s 超时)。
路由层 `/api/v1/comfy/object-info` 那份整表缓存是给编辑器用的,服务层不反向 import 它。

取不到(sidecar 不可达 / 超时 / 不认识的类型)一律返回 None,**绝不抛** —— 调用方对 None
走保守分支(删节点)。成功缓存 10 分钟(节点定义只在装/升插件时变),失败只缓存 30 秒。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import Any

import httpx

from src.services.comfy.client import ComfyError, get_comfy_client

logger = logging.getLogger(__name__)

OBJECT_INFO_TTL_S = 600.0
OBJECT_INFO_FAILURE_TTL_S = 30.0
OBJECT_INFO_MAX_ENTRIES = 512
OBJECT_INFO_FETCH_TIMEOUT_S = 5.0

# class_type → (取回时刻(monotonic), info | None)。None = 取不到(负缓存)。
_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def reset_object_info_cache() -> None:
    """测试/重配置用。"""
    _cache.clear()


def _cache_get(class_type: str) -> tuple[bool, dict[str, Any] | None]:
    hit = _cache.get(class_type)
    if hit is None:
        return False, None
    ts, info = hit
    ttl = OBJECT_INFO_TTL_S if info is not None else OBJECT_INFO_FAILURE_TTL_S
    if time.monotonic() - ts >= ttl:
        _cache.pop(class_type, None)
        return False, None
    return True, info


def _cache_put(class_type: str, info: dict[str, Any] | None) -> None:
    _cache.pop(class_type, None)
    while len(_cache) >= OBJECT_INFO_MAX_ENTRIES:
        _cache.pop(next(iter(_cache)))
    _cache[class_type] = (time.monotonic(), info)


async def _fetch(class_type: str) -> dict[str, Any] | None:
    try:
        return await get_comfy_client().node_info(
            class_type, timeout=OBJECT_INFO_FETCH_TIMEOUT_S)
    except (httpx.HTTPError, ComfyError, ValueError) as e:
        logger.warning("comfy object_info: 取 %r 失败,按取不到处理:%s", class_type, e)
        return None


async def get_node_infos(class_types: Iterable[str]) -> dict[str, dict[str, Any] | None]:
    """`{class_type: info | None}`,每个类型最多打一次 sidecar(并发取)。"""
    wanted = list(dict.fromkeys(str(c) for c in class_types))
    out: dict[str, dict[str, Any] | None] = {}
    misses: list[str] = []
    for ct in wanted:
        hit, info = _cache_get(ct)
        if hit:
            out[ct] = info
        else:
            misses.append(ct)
    if misses:
        fetched = await asyncio.gather(*(_fetch(ct) for ct in misses))
        for ct, info in zip(misses, fetched, strict=True):
            _cache_put(ct, info)
            out[ct] = info
    return out
