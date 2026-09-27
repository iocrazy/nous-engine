import asyncio
import logging
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from functools import lru_cache

from pydantic import BaseModel

from src.api.deps_admin import require_admin
from src.api.response_cache import cached, invalidate
from src.config import LAUNCH_PARAM_WHITELIST
from src.errors import ConflictError, EngineUnloadRefusedError
from src.services.model_scanner import scan_models, _VLLM_ADAPTER
from src.gpu.detector import gpu_summary
from src.models.database import get_async_session
from src.models.schemas import EngineInfo, EngineLoadResponse
from src.services.model_metadata_service import (
    get_all_metadata, sync_metadata, refresh_metadata, scan_local_models,
    _format_size,
)
from src.api.websocket import ws_manager
# 模块整体导入(而非 from-import 具名):删除路由里的函数在测试中按模块属性打桩,
# 早绑定会拿到打补丁前的旧引用。
from src.services import model_deleter as md

# 持后台 fire-and-forget task 的强引用,防止 asyncio 中途 GC 掉(round4 #8/#9 修过同类)。
# add_done_callback(discard) 完成即自动移除。
_bg_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t

router = APIRouter(prefix="/api/v1/engines", tags=["engines"])
# GPU 拓扑(卡组)另起一个前缀:它讲的是**机器的卡**,不是某个引擎 —— 挂在
# /api/v1/engines 下会读成"引擎的组"。main.py 一并 include。
gpu_router = APIRouter(prefix="/api/v1/gpu", tags=["gpu"])
logger = logging.getLogger(__name__)

# In-memory loading state tracker: model_id -> {"status": "loading"|"failed", "detail": str}
_loading_states: dict[str, dict[str, str]] = {}


@router.get("/gpus")
async def list_gpus():
    """Return detected GPU information."""
    return gpu_summary()


def _get_model_manager(request: Request):
    return getattr(request.app.state, "model_manager", None)


def _is_engine_loaded(name: str, request: Request | None = None) -> bool:
    if request is not None:
        mgr = _get_model_manager(request)
        if mgr is not None:
            return mgr.is_loaded(name)
    # Fallback to old registries
    from src.workers.tts_engines import registry as tts_registry
    from src.workers.llm_engines import registry as llm_registry
    engine = tts_registry._ENGINE_INSTANCES.get(name)
    if engine is not None:
        return engine.is_loaded
    engine = llm_registry._ENGINE_INSTANCES.get(name)
    if engine is not None:
        return engine.is_loaded
    return False


def _get_loaded_gpu(name: str, request: Request | None = None) -> int | None:
    if request is not None:
        mgr = _get_model_manager(request)
        if mgr is not None and mgr.is_loaded(name):
            lm = mgr._models.get(name)
            return lm.gpu_index if lm else None
    return None


def _get_loaded_gpus(name: str, request: Request | None = None) -> list[int] | None:
    if request is not None:
        mgr = _get_model_manager(request)
        if mgr is not None and mgr.is_loaded(name):
            lm = mgr._models.get(name)
            if lm and lm.gpu_indices:
                return lm.gpu_indices
            elif lm:
                return [lm.gpu_index]
    return None


def _get_held_by(name: str, request: Request | None = None) -> list[str]:
    """spec 2026-09-05 §9:当前持有该模型的活跃引用(排序后的 owner 串)。

    未加载 / 拿不到 manager → 空列表(老 registry 回退路径没有引用计数这回事)。
    """
    if request is not None:
        mgr = _get_model_manager(request)
        if mgr is not None:
            return sorted(mgr.get_references(name))
    return []


def _build_engine_info(key: str, cfg: dict, meta, local_dirs: set[str], request: Request | None = None) -> EngineInfo:
    local_path = cfg.get("local_path")
    local_exists = local_path in local_dirs if local_path else False
    loaded = _is_engine_loaded(key, request)

    # Determine status: check loading states first, then fall back to loaded/unloaded
    loading_state = _loading_states.get(key)
    if loading_state:
        status = loading_state["status"]
        status_detail = loading_state.get("detail", "")
    elif loaded:
        status = "loaded"
        status_detail = None
    else:
        status = "unloaded"
        status_detail = None

    # 字段规则(审查 #6):`gpu` **永远是主卡 int**(组的第一张),`gpus` 是**唯一**的
    # 列表字段(单卡 → None)。此前配了组时 `gpu` 也被设成同一个 list,前端得在菜单和
    # 卡片各 `Array.isArray` 判一次组,两处判据还可能不一致。
    from src.gpu.topology import resolve_gpus
    configured = resolve_gpus(cfg)          # gpu/gpus 优先级的唯一实现
    gpu_group = configured if len(configured) > 1 else None
    if configured:
        gpu_field = configured[0]
    else:
        from src.gpu.detector import get_device_for_engine
        device = get_device_for_engine(cfg)
        try:
            gpu_field = int(device.split(":")[-1]) if device.startswith("cuda") else 0
        except (ValueError, IndexError):
            gpu_field = 0

    info = EngineInfo(
        name=key,
        display_name=cfg["name"],
        type=cfg["type"],
        status=status,
        gpu=gpu_field,
        gpus=gpu_group,
        supports_gpu_group=_adapter_supports_gpu_group(cfg.get("adapter")),
        vram_gb=cfg.get("vram_gb", 0),
        resident=cfg.get("resident", False),
        local_path=local_path,
        local_exists=local_exists,
        auto_detected=cfg.get("auto_detected", False),
        has_adapter=bool(cfg.get("adapter")),
        loaded_gpu=_get_loaded_gpu(key, request) if loaded else None,
        loaded_gpus=_get_loaded_gpus(key, request) if loaded else None,
        held_by=_get_held_by(key, request) if loaded else [],
        status_detail=status_detail,
    )
    if meta:
        info.organization = meta.organization
        info.model_size = _format_size(meta.model_size_bytes)
        info.frameworks = meta.frameworks
        info.libraries = meta.libraries
        info.license = meta.license
        info.languages = meta.languages
        info.tags = meta.tags
        info.tensor_types = meta.tensor_types
        info.description = meta.description
        info.has_metadata = True
    return info


@router.get("")
@cached("engines", ttl=30)
async def list_all_engines(
    request: Request,
    type: str | None = None,
    session: AsyncSession = Depends(get_async_session),
):
    """List all engines with metadata. Optionally filter by type.

    Cached 30s in-process. Cache key includes ``?type=`` filter via the cache
    layer's query-string handling. Writes to engine state (load/unload/scan/
    resident/gpu/install_deps and the background loaders) all call
    ``invalidate("engines")`` to drop the cached body before the next read.
    """
    configs = scan_models()
    metadata = await get_all_metadata(session)
    local_dirs = scan_local_models()
    result = []
    for key, cfg in configs.items():
        if type and cfg.get("type") != type:
            continue
        # Only show models that exist locally
        local_path = cfg.get("local_path")
        if not local_path or local_path not in local_dirs:
            continue
        result.append(_build_engine_info(key, cfg, metadata.get(key), local_dirs, request))
    return result


@router.post("/scan", dependencies=[Depends(require_admin)])
async def scan_models_endpoint():
    """Re-scan models directory for new models.

    Response shape distinguishes **配置识别** vs **本地可用**:用户报告 toast
    显示「扫到 25」结果引擎库只有 16,差异在于 yaml 配的 9 个模型本地没下载,
    被 list_all_engines 在 `local_path not in local_dirs` 过滤。本接口把两个
    数字一起返回,前端可以拼出「识别 N · 本地可用 K · 未下载 (N-K)」消除误导。
    """
    # 显式 /scan 意图就是"现在重扫" → 先清 TTL 缓存,强制走盘一次(否则最长 30s 内看不到
    # 新模型)。
    from src.services.model_scanner import invalidate_scan_cache
    from src.services.model_metadata_service import invalidate_local_scan_cache
    invalidate_scan_cache()
    invalidate_local_scan_cache()  # scan_local_models 也有 30s 缓存,一并清
    configs = scan_models()
    local_dirs = scan_local_models()
    # configs 已包含 local_path;在 local_dirs 中即「磁盘上真有目录的模型」。
    local_available = sum(
        1 for cfg in configs.values()
        if cfg.get("local_path") and cfg["local_path"] in local_dirs
    )
    invalidate("engines")
    return {
        "count": len(configs),
        "local_available": local_available,
        "not_local": len(configs) - local_available,
        "models": list(configs.keys()),
    }


@router.post("/reload", dependencies=[Depends(require_admin)])
async def reload_registry(request: Request):
    """热加载模型定义,不重启即认新模型。

    重读单一来源 collect_model_entries(`configs/models.d/*.yaml` 一模型一文件
    + 兼容 `models.yaml` 的 legacy `models:` list)→ 丢个新 `<id>.yaml` 进 models.d
    后调本接口即可插拔上线,无需重启后端。
    """
    mgr = _get_model_manager(request)
    if mgr is None:
        raise HTTPException(503, "ModelManager not initialized")
    new_count = mgr._registry.reload()
    invalidate("engines")
    return {"status": "reloaded", "new_models": new_count, "total": len(mgr._registry.specs)}


@router.post("/sync-metadata", dependencies=[Depends(require_admin)])
async def sync_all_metadata(session: AsyncSession = Depends(get_async_session)):
    """Fetch metadata for any engine not yet in DB."""
    metadata = await sync_metadata(session)
    invalidate("engines")
    return {"synced": len(metadata)}


@router.post("/{name}/refresh-metadata", response_model=EngineInfo, dependencies=[Depends(require_admin)])
async def refresh_engine_metadata(
    name: str,
    session: AsyncSession = Depends(get_async_session),
):
    """Force re-fetch metadata for a specific engine."""
    configs = scan_models()
    if name not in configs:
        raise HTTPException(404, detail=f"Unknown engine: {name}")
    meta = await refresh_metadata(session, name)
    local_dirs = scan_local_models()
    invalidate("engines")
    return _build_engine_info(name, configs[name], meta, local_dirs)


# 注:必须在 `/{name}/unload` 之前定义,否则参数路由 `/{name}/unload` 抢先匹配致 404。
@router.post("/loaded-adapter/unload", dependencies=[Depends(require_admin)])
async def unload_loaded_adapter(request: Request, body: dict = Body(...)):
    """卸载**已加载的 combo adapter**(引擎库「已加载」卡的卸载按钮,统一模型管理收尾 PR-2)。

    body: `model_id`(combo 哈希 id,/loaded-adapters 列的)。按 aggregate_runner_loaded 找它所在
    runner group → 向该 runner 派 UnloadModel + 对账快照。combo = 工作流动态组装的单文件(unet+clip+vae)
    /anima/SeedVR2 等,model_id 是哈希。没加载则 no-op。状态经下个 Pong 反映。"""
    from src.services.runner_models import aggregate_runner_loaded  # noqa: PLC0415
    model_id = str(body.get("model_id") or "")
    if not model_id:
        raise HTTPException(422, "需要 model_id(/loaded-adapters 列的 combo id)")
    state = request.app.state
    sups_by_group = {
        getattr(s, "group_id", None): s
        for s in (getattr(state, "runner_supervisors", None) or [])
    }
    unloaded = False
    for e in aggregate_runner_loaded(state):
        if str(e.get("model_id") or "") != model_id:
            continue
        sup = sups_by_group.get(e.get("group_id"))
        if sup is not None and getattr(sup, "client", None) is not None:
            try:
                await sup.client.unload_model(model_id)
                unloaded = True
                if hasattr(sup, "_reconcile_loaded"):
                    await sup._reconcile_loaded()
            except Exception as ex:  # noqa: BLE001 — 单个失败不挡
                logger.warning("unload loaded-adapter %s failed: %s", model_id, ex)
        break
    invalidate("engines")
    return {"status": "accepted", "model_id": model_id, "unloaded": unloaded}


@router.post("/{name}/load", response_model=EngineLoadResponse, dependencies=[Depends(require_admin)])
async def load_engine(name: str, request: Request):
    configs = scan_models()
    if name not in configs:
        raise HTTPException(404, detail=f"Unknown engine: {name}")

    cfg = configs[name]
    # Refuse to start a load that we know will crash. Auto-detected
    # diffusers (image/video) ship without an adapter — letting the
    # background task run only to ValueError("Unknown model") gives
    # the user a stale toast and a "failed" badge with no path forward.
    if not cfg.get("adapter"):
        raise HTTPException(
            422,
            detail=(
                f"'{name}' was auto-detected on disk but has no adapter "
                f"configured. Image/video diffusers loading is not "
                f"implemented yet — add an adapter entry to "
                f"backend/configs/models.yaml to enable loading."
            ),
        )

    # Reject if already loading
    if name in _loading_states and _loading_states[name]["status"] == "loading":
        return EngineLoadResponse(name=name, status="loading")

    model_mgr = request.app.state.model_manager

    # If already loaded, return immediately
    if model_mgr.is_loaded(name):
        return EngineLoadResponse(name=name, status="loaded")

    # Start background loading
    _loading_states[name] = {"status": "loading", "detail": "Starting..."}
    invalidate("engines")
    _spawn_bg(_load_in_background(name, model_mgr))

    return EngineLoadResponse(name=name, status="loading")


async def _load_in_background(name: str, model_mgr):
    import logging
    logger = logging.getLogger(__name__)
    start = time.monotonic()
    try:
        _loading_states[name] = {"status": "loading", "detail": "Loading model..."}
        await ws_manager.broadcast_model_status(name, "loading", "Loading model...")
        await model_mgr.load_model(name)
        elapsed = round(time.monotonic() - start, 2)
        # Clear loading state on success — the model is now truly loaded
        _loading_states.pop(name, None)
        # Invalidate cache so the next /engines GET reflects the new status
        # (background task changes status without going through an HTTP write).
        invalidate("engines")
        await ws_manager.broadcast_model_status(name, "loaded", f"Ready ({elapsed}s)")
        logger.info("Model %s loaded in %.2fs", name, elapsed)
    except Exception as e:
        _loading_states[name] = {"status": "failed", "detail": str(e)}
        invalidate("engines")
        await ws_manager.broadcast_model_status(name, "failed", str(e))
        logger.error("Model %s load failed: %s", name, e)


@router.post("/{name}/unload", response_model=EngineLoadResponse, dependencies=[Depends(require_admin)])
async def unload_engine(name: str, request: Request, force: bool = False):
    configs = scan_models()
    if name not in configs:
        raise HTTPException(404, detail=f"Unknown engine: {name}")

    cfg = configs[name]
    if cfg.get("resident", False) and not force:
        # 2026-09-05:裸 HTTPException(409) 只会被全局 handler 渲成 code="conflict",
        # 与同一端点另外两条 409(engine_in_use / engine_referenced)不同构,
        # 调用方没法按 code 分流。三条拒绝理由都给自己的 code + fix。
        raise ConflictError(
            f"Engine {name} is resident; not unloaded.",
            code="engine_resident",
            fix=f"POST /api/v1/engines/{name}/unload?force=true",
        )

    model_mgr = request.app.state.model_manager
    ok = await model_mgr.unload_model(name, force=force)
    # spec 2026-09-05 §8:接住返回值。此前这里无条件报 "unloaded",而 unload_model 在
    # 有引用 / in_use 时 return False 且只打 debug —— 真机表现是 200 + 进程活着 + 显存不退。
    # 「没加载」的 False 仍是 no-op 200(与 test_unload_non_loaded_engine 一致)。
    if ok is False and model_mgr.is_loaded(name):
        # 判定顺序必须与 unload_model 内部一致:**先 in_use、后引用**。in-use 是
        # 无条件的第一道硬守卫(卸载正在 infer 的 adapter → segfault),**强于 force**;
        # 一个模型可以既被引用又正在 infer,先看 refs 就会误报 engine_referenced +
        # 建议 force,调用方照做仍是 409 —— 死循环(2026-09-05 审查)。
        if model_mgr.is_in_use(name):
            raise ConflictError(
                f"Engine {name} is in use (inference in flight); not unloaded.",
                code="engine_in_use",
                fix="wait for in-flight requests to finish, then retry; force=true does not override in_use",
            )
        refs = sorted(model_mgr.get_references(name))
        if refs:
            # referenced_by 是结构化字段(spec §8),不是只把 refs 拼进 message ——
            # 调用方要能直接读列表,而不是去正则解析一句话。
            raise EngineUnloadRefusedError(
                f"Engine {name} is referenced by {refs}; not unloaded.",
                code="engine_referenced",
                fix=f"POST /api/v1/engines/{name}/unload?force=true",
                referenced_by=[str(r) for r in refs],
            )
        # 理论上到不了:unload_model 的拒绝分支只有 in_use / resident / refs 三条,
        # resident 在本函数开头已挡(force 过来的 resident 不会被拒)。真到了说明
        # manager 侧新增了一条路由不认识的拒绝理由 —— 别再猜原因,如实说不知道。
        raise ConflictError(
            f"Engine {name} was refused unload by the model manager for an unknown reason.",
            code="engine_unload_refused",
            fix="check backend logs (journalctl -u nous-engine-backend) for the 'Skipping unload' line",
        )

    # round9 BUG4:清掉残留的 loading/failed 状态。_build_engine_info 里
    # _loading_states 优先级高于 loaded/unloaded —— load 失败写了 {"status":"failed"}
    # 后从不被清(unload 旧实现不 pop),GET /engines 会永远显示 "failed",哪怕重新
    # unload 也甩不掉。卸载即代表该 engine 回到干净 unloaded 态,这里 pop 掉。
    _loading_states.pop(name, None)

    invalidate("engines")
    return EngineLoadResponse(name=name, status="unloaded")


@router.get("/loaded-adapters", dependencies=[Depends(require_admin)])
async def list_loaded_adapters(request: Request):
    """Bug 3 PR-2c:列所有 runner 子进程加载的 combo adapter 实体(image/tts),供引擎库
    「已加载」tab 渲染。

    它们是工作流动态组装的单文件 combo(unet+clip+vae,model_id 是哈希)—— 不对应
    注册卡片,所以不能靠 engines 列表的 status='loaded' 显示。前端在「已加载」tab 把这些
    作为独立实体卡渲染(源文件 basename + GPU + VRAM + pipeline_class)。

    只返 runner 上报的(group_id != 'main');主进程内模型(LLM)本就以注册卡形式出现在
    engines 列表,不在此重复。
    """
    from src.services.runner_models import aggregate_runner_loaded
    import os
    entries = []
    for e in aggregate_runner_loaded(request.app.state):
        if e.get("group_id") == "main":
            continue
        srcs = e.get("source_files") or []
        entries.append({
            "model_id": e.get("model_id"),
            "model_type": e.get("model_type"),
            "group_id": e.get("group_id"),
            "gpu_index": e.get("gpu_index"),
            "vram_mb": e.get("vram_mb"),
            "pipeline_class": e.get("pipeline_class"),
            "source_files": srcs,
            # 人读名:取 diffusion_models(首个源文件)basename;无则退回 model_id。
            "display_name": os.path.basename(srcs[0]) if srcs else e.get("model_id"),
            "last_used_ago_sec": e.get("last_used_ago_sec"),
        })
    return {"count": len(entries), "entries": entries}


_install_states: dict[str, dict[str, str]] = {}  # engine -> {status, detail}


@router.get("/deps")
async def list_engine_deps():
    """Return install/probe status for every TTS engine in the manifest."""
    from src.services.tts_deps import list_manifest
    data = list_manifest()
    # overlay any in-flight install state
    for k, v in _install_states.items():
        if k in data:
            data[k]["install_state"] = v
    return data


@router.post("/{name}/install_deps", dependencies=[Depends(require_admin)])
async def install_engine_deps(name: str):
    """Install pip deps for a TTS engine (background). Status pushed via ws."""
    from src.services.tts_deps import get as get_dep
    if get_dep(name) is None:
        raise HTTPException(404, detail=f"No dep manifest for engine: {name}")
    state = _install_states.get(name)
    if state and state.get("status") == "installing":
        return {"name": name, "status": "installing"}
    _install_states[name] = {"status": "installing", "detail": "Starting..."}
    invalidate("engines")
    _spawn_bg(_install_in_background(name))
    return {"name": name, "status": "installing"}


async def _install_in_background(name: str):
    import logging as _lg
    from src.services.tts_deps import install
    log = _lg.getLogger(__name__)
    await ws_manager.broadcast_model_status(name, "installing", "Installing deps...")

    async def _push(line: str):
        # Throttle: only push lines that look meaningful (avoid noise)
        if any(k in line.lower() for k in ("collecting", "downloading", "installing", "successfully", "error")):
            _install_states[name] = {"status": "installing", "detail": line[:200]}
            await ws_manager.broadcast_model_status(name, "installing", line[:200])

    try:
        ok, output = await install(name, on_log=_push)
        if ok:
            _install_states[name] = {"status": "installed", "detail": "Install complete"}
            invalidate("engines")
            await ws_manager.broadcast_model_status(name, "installed", "Install complete")
            log.info("TTS deps installed for %s", name)
        else:
            tail = "\n".join(output.splitlines()[-5:])
            _install_states[name] = {"status": "install_failed", "detail": tail}
            invalidate("engines")
            await ws_manager.broadcast_model_status(name, "install_failed", tail)
            log.error("TTS dep install failed for %s: %s", name, tail)
    except Exception as e:
        _install_states[name] = {"status": "install_failed", "detail": str(e)}
        invalidate("engines")
        await ws_manager.broadcast_model_status(name, "install_failed", str(e))
        log.exception("TTS dep install crashed for %s", name)


@router.patch("/{name}/resident", dependencies=[Depends(require_admin)])
async def set_resident(name: str, request: Request, resident: bool = True,
                       session: AsyncSession = Depends(get_async_session)):
    """Toggle 常驻(随启动预加载 + 不被 TTL/LRU 自动卸)for an engine.

    两个修复:
    1. **持久到 DB 表 model_runtime_overrides**(数据加载统一 2026-06-16;不碰 git 跟踪的
       models.yaml)—— 否则 UI 设的常驻写进 yaml 是未提交本地改动,git checkout/pull/reset 冲掉。
    2. **立即作用到已加载实例**(set_model_resident,no-op if 未加载)—— 否则当前加载着的模型标常驻后
       内存 spec.resident 没变,仍被 TTL 卸,常驻只下次加载才生效(用户报告「标常驻还是被卸」)。
    """
    from src.config import load_model_configs
    from src.services import runtime_override_store

    if name not in load_model_configs():
        raise HTTPException(404, detail=f"Unknown engine: {name}")

    await runtime_override_store.set_override(session, name, "resident", resident)

    mgr = getattr(request.app.state, "model_manager", None)
    if mgr is not None:
        mgr.set_model_resident(name, resident)

    invalidate("engines")
    return {"name": name, "resident": resident}


#: 白名单的定义在 `src/config.py`(配置层),这里只是本模块内的短名。
#: 定义搬过去的理由:消费方除了本路由,还有 `ModelManager._instantiate_adapter`
#: (服务层)—— 让服务层反向 import API 层,方向是反的,且将来 engines.py 顶层多一个
#: import 就可能变成真循环,而报错会落在「加载模型」这条完全无关的路径上。
_LAUNCH_PARAM_WHITELIST = LAUNCH_PARAM_WHITELIST

#: 这些键单独给理由,不要混在"不在白名单"的通用报错里 —— 用户会以为是拼错了。
_LAUNCH_PARAM_REDIRECTS = {
    "gpu_memory_utilization":
        "显存预算请改用 PATCH /engines/{name}/vram-budget(mode=absolute + 绝对 GiB)。"
        "gpu_memory_utilization 是「占该卡总量」的比例,换卡必须重算,不作为可编辑项。",
    "tensor_parallel_size":
        "tp 由放置决定(ModelManager._resolve_placement),不是调优旋钮。"
        "要换卡/换组请用 PATCH /engines/{name}/gpu。",
}


#: 白名单内每个键的值域。只校验键名不校验值是不够的:前端 `Number('')` 是 `0`,
#: 清空输入框就会把 `max_model_len: 0` 写进库,而 0 让 vLLM 起不来 —— 库里躺着一个
#: 起不来的值,还得人去 DB 里捞。"哪个键、收到了什么、要求是什么"三样都要说清楚,
#: 否则前端只能把 422/400 原样弹给用户。
_LAUNCH_PARAM_VALIDATORS: dict[str, tuple] = {
    # (判定, 人话要求)。None(清除覆盖)在调用处统一放行,不进这里。
    "max_model_len": (lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
                      "正整数"),
    "max_num_seqs": (lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
                     "正整数"),
    "max_num_batched_tokens": (
        lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0, "正整数"),
    "enable_prefix_caching": (lambda v: isinstance(v, bool), "布尔值"),
    "dtype": (lambda v: isinstance(v, str) and v.strip() != "", "非空字符串"),
    "quantization": (lambda v: isinstance(v, str) and v.strip() != "", "非空字符串"),
}


def _editable_launch_params(cfg: dict) -> frozenset[str]:
    """该引擎的适配器**真正消费得了**的白名单键。

    判据是适配器 `__init__` 的形参表,不是模型的 type —— MOSS ASR(type=asr)走
    `SGLangOmniAdapter`,签名以 `**kwargs` 收尾,6 个键一个都不接;TTS 那几个引擎同理。
    按 type 猜的话面板对它们是开着的:写得进库、GET 报「已覆盖」、引擎行为纹丝不动
    ——正是这一支从头在修的那类 bug(2026-09-22 复查 N1)。

    同形状的先例:`ModelManager._instantiate_adapter` 也是按
    `inspect.signature(cls.__init__).parameters` 决定要不要传 `vram_budget` / `gpus`。

    `**kwargs` **不算**接受:它只是把参数吞掉,不会有任何效果。
    """
    import importlib
    import inspect

    dotted = cfg.get("adapter") or ""
    if "." not in dotted:
        return frozenset()
    module_path, _, class_name = dotted.rpartition(".")
    try:
        cls = getattr(importlib.import_module(module_path), class_name)
        sig = inspect.signature(cls.__init__)
    except (ImportError, AttributeError, ValueError, TypeError) as e:
        # 取不到签名就别拦(fail-open):拦错了用户连改都改不了,比多给一个无效按钮更糟。
        logger.warning("launch-params: 取不到 %s 的签名,不做可编辑性收窄:%s", dotted, e)
        return _LAUNCH_PARAM_WHITELIST
    return frozenset(k for k in _LAUNCH_PARAM_WHITELIST if k in sig.parameters)


def _validate_launch_param_values(body: dict) -> None:
    """值域校验。`None` = 清除该覆盖,任何键都放行。违反 → 400。

    ⚠️ `bool` 是 `int` 的子类:`isinstance(True, int)` 为真,不显式排除的话
    `max_num_seqs: true` 会被当成 1 存下去。
    """
    for k, v in body.items():
        if v is None:
            continue
        check = _LAUNCH_PARAM_VALIDATORS.get(k)
        if check is None:      # 白名单已经拦过未知键,这里是双保险
            continue
        ok, want = check
        if not ok(v):
            raise HTTPException(
                400,
                detail=f"{k} 的值非法:收到 {v!r}({type(v).__name__}),要求 {want};"
                       f"传 null 表示清除该覆盖、回退 models.d 的 yaml 值",
            )


@router.patch("/{name}/launch-params", dependencies=[Depends(require_admin)])
async def set_launch_params(
    name: str,
    body: dict,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    """覆盖模型的启动参数,持久到 DB 的 model_runtime_overrides.params。

    **不写 configs/models.yaml** —— 2026-06-20 起模型定义在 models.d/,那个文件只剩
    空锚点;而且写 git 跟踪的 yaml 会被 git checkout/pull 冲掉(同 resident 端点的理由)。

    值为 `null` 的键 = **清除该覆盖**(回退 models.d 的 yaml 值)。
    其余值过 `_LAUNCH_PARAM_VALIDATORS` 的值域校验(数字键必须 > 0)。
    改动只在**下次 load** 时读到,所以 `applied` 恒为 false。

    白名单之外还有第二道:该引擎的**适配器**接不接受这个键(`_editable_launch_params`)。
    不接受就 400,不是静静存起来 —— 存了也到不了引擎。
    """
    from src.config import load_model_configs
    from src.services import runtime_override_store

    cfg = load_model_configs().get(name)
    if cfg is None:
        raise HTTPException(404, detail=f"Unknown engine: {name}")

    for k, hint in _LAUNCH_PARAM_REDIRECTS.items():
        if k in body:
            raise HTTPException(400, detail=hint.format(name=name))

    bad = [k for k in body if k not in _LAUNCH_PARAM_WHITELIST]
    if bad:
        raise HTTPException(
            400,
            detail=f"不可覆盖的参数 {bad};允许:{sorted(_LAUNCH_PARAM_WHITELIST)}",
        )
    if not body:
        raise HTTPException(400, detail="body 为空;至少给一个参数")

    editable = _editable_launch_params(cfg)
    # `null` = **清除覆盖**,任何键都放行,哪怕该适配器吃不下它。
    # 否则库里躺着的历史死数据(收窄之前的 200 存下的、到不了引擎的覆盖)就永远清不掉:
    # 面板不渲染它、PATCH 又拒绝它,只剩手改 DB 一条路。清除只会让状态更干净,
    # 不可能让引擎收到它不认识的参数 —— 拦它没有任何收益。
    unusable = sorted(k for k, v in body.items() if v is not None and k not in editable)
    if unusable:
        adapter = cfg.get("adapter") or "(无)"
        if editable:
            detail = (
                f"引擎 {name} 的适配器({adapter})不接受 {unusable};"
                f"它能接受的是 {sorted(editable)}"
            )
        else:
            detail = (
                f"引擎 {name} 不支持运行时调整启动参数 —— 它的适配器({adapter})"
                f"的 __init__ 一个白名单键都不接受,写进库也到不了引擎。"
                f"要调它的启动配置请改该引擎自己的配置文件后重新加载。"
            )
        raise HTTPException(400, detail=detail)

    _validate_launch_param_values(body)

    await runtime_override_store.set_override(session, name, "params", body)

    # services 列表带 capabilities.context(= max_model_len),同样要失效。
    invalidate("engines", "services")
    merged = runtime_override_store.get_overrides().get(name, {}).get("params", {})
    return {
        "name": name,
        "params": merged,
        "applied": False,
        "hint": "需重新加载模型生效(unload + load)",
    }


def _launch_params_view(params: dict) -> dict:
    """params → 只含白名单键的视图(effective 与 defaults 共用同一口径)。

    prefix caching 有**两条**配法:`params.enable_prefix_caching`(适配器 kwarg)和
    `params.vllm_args["enable-prefix-caching"]`(透传,同名时以它为准)。本机 models.d 里的
    qwen3.8 变体走的都是后者 —— 只读前者的话 UI 复选框会显示"没开",而实际是开着的,
    用户一点就把真实状态改掉了。所以补读 vllm_args。
    """
    view = {k: v for k, v in params.items() if k in _LAUNCH_PARAM_WHITELIST}
    va = params.get("vllm_args") or {}
    for alias in ("enable-prefix-caching", "enable_prefix_caching"):
        if alias in va:
            view["enable_prefix_caching"] = bool(va[alias])
            break
    return view


@router.get("/{name}/launch-params", dependencies=[Depends(require_admin)])
async def get_launch_params(name: str):
    """读该引擎**当前生效**的启动参数 + 哪几个键是运行时覆盖的。

    `effective` 已经过 `_apply_runtime_overrides` 的深合并(load_model_configs 内),
    所以它就是"下次 load 会用的值";`overridden` 标出其中哪些来自 DB 覆盖、
    哪些还是 models.d 的 yaml 值 —— UI 靠它显示"已改"标记 / 提供"恢复默认"。

    `editable` 是**这个引擎的适配器真吃得下**的那几个键(见 `_editable_launch_params`),
    前端据此决定渲染哪些控件 —— 空列表 = 这个引擎没有可调项,面板整个不该出现。

    只回白名单内的键:露出 gpu_memory_utilization 这类不可编辑项,迟早有人给它加输入框。
    """
    from src.config import load_model_configs
    from src.services import runtime_override_store

    cfg = load_model_configs().get(name)
    if cfg is None:
        raise HTTPException(404, detail=f"Unknown engine: {name}")

    effective = _launch_params_view(cfg.get("params") or {})
    # yaml 原值(不叠运行时覆盖):UI 显示「已覆盖为 X(yaml 默认 Y)」,改大了起不来时
    # 用户一眼知道退回哪里。2026-09-22 huihui 被点成 256K 起不来,面板上看不出原来是 32K。
    raw = load_model_configs(apply_overrides=False).get(name) or {}
    defaults = _launch_params_view(raw.get("params") or {})
    ov_params = runtime_override_store.get_overrides().get(name, {}).get("params") or {}
    overridden = sorted(k for k in ov_params if k in _LAUNCH_PARAM_WHITELIST)
    return {
        "name": name,
        "effective": effective,
        "defaults": defaults,
        "overridden": overridden,
        "editable": sorted(_editable_launch_params(cfg)),
        "hint": "改动需重新加载模型生效(unload + load)",
    }


def _card_total_gb_for_engine(cfg: dict, loaded_gpu: int | None = None) -> float:
    """预算分母 = 目标卡的总显存(GB)。优先级:**真实落卡(已加载)> 配置钉的卡/组 >
    detector 推断** → 回退 24。

    组(张量并行)按组内**最小** total 算,不是求和:`gpu_memory_utilization` 是**每卡**
    比例。数据源统一走 `topology.group_budget_gb`(nvidia-smi 的 MB)—— 与适配器同一个
    实现、同一个数据源。此前这里用 `gpu_summary()`(torch 的 total_memory,四舍五入到
    0.1G)、适配器用 nvidia-smi,同一张卡两个数,预算端点放行的绝对值适配器可能算超,
    正是注释里警告的那种启动期 OOM(审查 #4)。
    """
    from src.gpu.topology import group_budget_gb, resolve_gpus

    if loaded_gpu is not None:
        total, _free = group_budget_gb([loaded_gpu])
        if total > 0:
            return total

    cards = resolve_gpus(cfg)
    if not cards:
        from src.gpu.detector import get_device_for_engine
        try:
            dev = get_device_for_engine(cfg)
            cards = [int(dev.split(":")[-1])] if dev.startswith("cuda") else [0]
        except (ValueError, IndexError):
            cards = [0]
    total, _free = group_budget_gb(cards)
    return total or 24.0


# _VLLM_ADAPTER 单一来源在 model_scanner(顶部 import;去重:此前两处各定义一份同值)。


@router.get("/{name}/vram-budget", dependencies=[Depends(require_admin)])
async def get_vram_budget(name: str, request: Request):
    """返回该引擎当前显存预算设置 + 推荐值 + 目标卡总显存(spec 2026-06-13)。
    仅 vLLM 类(llm/embedding/vl)有意义;image/tts(非 vLLM)返回 applicable=false。"""
    from src.config import load_model_configs, load_runtime_overrides, recommend_vram_budget_gb

    cfgs = load_model_configs()
    cfg = cfgs.get(name)
    if cfg is None:
        raise HTTPException(404, detail=f"Unknown engine: {name}")

    applicable = cfg.get("adapter") == _VLLM_ADAPTER
    card_total_gb = _card_total_gb_for_engine(cfg, _get_loaded_gpu(name, request))
    weights_gb = float(cfg.get("vram_gb") or 0)  # load_model_configs 已把 vram_mb 转成 vram_gb
    rec_gb = recommend_vram_budget_gb(cfg.get("type", "llm"), weights_gb)
    rec_percent = round(min(0.98, rec_gb / card_total_gb), 3) if card_total_gb > 0 else None

    current = (load_runtime_overrides().get(name) or {}).get("vram_budget") or {"mode": "auto"}
    return {
        "name": name,
        "applicable": applicable,
        "current": current,
        "recommended_gb": rec_gb,
        "recommended_percent": rec_percent,
        "card_total_gb": round(card_total_gb, 1),
        "yaml_gpu_memory_utilization": (cfg.get("params") or {}).get("gpu_memory_utilization"),
    }


@router.patch("/{name}/vram-budget", dependencies=[Depends(require_admin)])
async def set_vram_budget(name: str, request: Request, body: dict = Body(...),
                          session: AsyncSession = Depends(get_async_session)):
    """写每模型显存预算到 DB(model_runtime_overrides;数据加载统一 2026-06-16)。
    body: {mode, value}。mode=auto 清除走 adapter 公式;percent(0–1)/absolute(GB)。需重载生效。"""
    from src.config import load_model_configs
    from src.services import runtime_override_store

    cfg = load_model_configs().get(name)
    if cfg is None:
        raise HTTPException(404, detail=f"Unknown engine: {name}")

    mode = str(body.get("mode") or "auto").lower()
    if mode not in ("auto", "percent", "absolute"):
        raise HTTPException(400, detail="mode must be auto|percent|absolute")

    if mode == "auto":
        await runtime_override_store.set_override(session, name, "vram_budget", {"mode": "auto"})
        invalidate("engines")
        return {"name": name, "vram_budget": {"mode": "auto"}, "applied": False,
                "hint": "需重新加载模型生效(unload + load)"}

    val = body.get("value")
    if not isinstance(val, (int, float)) or val <= 0:
        raise HTTPException(400, detail="value must be a positive number")
    if mode == "percent" and not (0 < val <= 0.98):
        raise HTTPException(400, detail="percent value must be in (0, 0.98]")
    if mode == "absolute":
        card_total_gb = _card_total_gb_for_engine(cfg, _get_loaded_gpu(name, request))
        if val > card_total_gb:
            raise HTTPException(
                400, detail=f"absolute {val}GB exceeds card total {card_total_gb:.1f}GB")

    budget = {"mode": mode, "value": float(val)}
    await runtime_override_store.set_override(session, name, "vram_budget", budget)
    invalidate("engines")
    return {"name": name, "vram_budget": budget, "applied": False,
            "hint": "需重新加载模型生效(unload + load)"}


class GpuAssignment(BaseModel):
    """PATCH /engines/{name}/gpu 的请求体 —— **只用于钉 GPU 组**。

    单卡走查询参数 `?gpu=N`(老路径,前端一直这么发),body 只承载 `gpus`。
    此前 body 里还有个 `gpu` 字段,没有任何调用方会发它,徒增一条优先级分支(审查 #6)。
    """

    gpus: list[int] | None = None


@router.patch("/{name}/gpu", dependencies=[Depends(require_admin)])
async def set_gpu(name: str, request: Request, gpu: int | None = None,
                  body: GpuAssignment | None = Body(None),
                  session: AsyncSession = Depends(get_async_session)):
    """Change GPU assignment for an engine —— 单卡或 GPU 组(张量并行)。

    写 DB 表 model_runtime_overrides(与 resident/vram_budget 一致;数据加载统一 2026-06-16)
    —— 不再写 git 跟踪的 models.yaml(旧行为污染 git 树、且 registry 读 yaml/overlay 口径分裂
    导致落卡设置不生效)。registry 套用覆盖 → spec.gpu / spec.gpus 驱动 vLLM 落卡。写后 reload
    registry 让 spec 立即刷新,随后 unload + load 即落新卡。

    组的合法性在这里就卡死(而不是等到 load 时炸):≥2 张、去重、卡存在、**同型号**。
    异构组队(3090 + PRO 6000)做张量并行要么白扔大卡显存要么直接 OOM,一律 400。
    """
    from src.config import load_model_configs
    from src.services import runtime_override_store

    cfg = load_model_configs().get(name)
    if cfg is None:
        raise HTTPException(404, detail=f"Unknown engine: {name}")

    group = list(body.gpus) if (body is not None and body.gpus) else None
    if group is not None:
        # 校验里有两次 nvidia-smi(显示卡探测 + 拓扑)—— 别在事件循环上跑(审查 #17)。
        group = await asyncio.to_thread(_validate_gpu_group, group, cfg)
        await runtime_override_store.set_override(session, name, "gpus", group)
        result = {"name": name, "gpu": group[0], "gpus": group}
    else:
        # set_override("gpu") 会把 gpus 写成 `[]`(显式清空组)—— 用户点单卡就是要退出组。
        await runtime_override_store.set_override(session, name, "gpu", int(gpu or 0))
        result = {"name": name, "gpu": int(gpu or 0), "gpus": None}

    # reload registry → spec.gpu/spec.gpus 从覆盖刷新(否则停在旧值,unload+load 仍落旧卡)。
    mgr = getattr(request.app.state, "model_manager", None)
    if mgr is not None and getattr(mgr, "_registry", None) is not None:
        mgr._registry.reload()
    invalidate("engines")
    return {**result, "applied": False, "hint": "需重新加载模型生效(unload + load)"}


@lru_cache(maxsize=64)
def _adapter_supports_gpu_group(dotted: str | None) -> bool:
    """这个引擎的适配器接不接受 GPU 组(张量并行)。见 InferenceAdapter.supports_gpu_group。

    只有 vLLM / SGLang 这类子进程型 LLM 适配器为 True。给别的引擎配组会造成
    manager 按两张卡各预留一半、`loaded_gpus` 显示 0+2,而适配器实际单卡跑 ——
    卡 2 的预留是幻影、UI 在撒谎(审查 #21)。
    """
    if not dotted:
        return False
    module_path, _, class_name = dotted.rpartition(".")
    if not module_path:
        return False
    try:
        import importlib
        return bool(getattr(getattr(importlib.import_module(module_path), class_name, None),
                            "supports_gpu_group", False))
    except Exception:  # noqa: BLE001 — 依赖没装的适配器不该让引擎列表 500
        return False


def _validate_gpu_group(raw: list[int], cfg: dict | None = None) -> list[int]:
    """校验 GPU 组 —— errors 抬成 400,warnings 打日志。

    校验规则本身在 `topology.validate_gpu_group`(**唯一实现**,YAML 加载路径也调它);
    这里只负责 HTTP 语义 + 适配器能力那条(引擎相关,不属于纯拓扑)。
    显示卡检查与单卡路径一致 —— 单卡钉到显示卡也只是 warning,不拒(审查 #2)。
    """
    from src.gpu.topology import validate_gpu_group

    if cfg is not None and not _adapter_supports_gpu_group(cfg.get("adapter")):
        raise HTTPException(
            400,
            detail=(
                f"该引擎的适配器不支持 GPU 组(张量并行):{cfg.get('adapter') or '(无)'}。"
                "只有 vLLM / SGLang 这类子进程型 LLM 引擎能跨卡加载;"
                "给别的引擎配组会造成'按两张卡预留、实际单卡跑'的幻影占用。"
            ),
        )
    gpus, errors, warnings = validate_gpu_group(raw)
    if errors:
        raise HTTPException(400, detail="; ".join(errors))
    for w in warnings:
        logger.warning("GPU 组分配:%s", w)
    return gpus


@gpu_router.get("/groups")
@cached("gpu-groups", ttl=60)
async def list_gpu_groups(request: Request):
    """可用于张量并行的 GPU 组候选 —— **hardware.yaml 里声明过的多卡 group**。

    `[{"id":"llm-tp","gpus":[0,2],"name":"RTX 3090","nvlink":true,"total_gb":48.0,
       "display_gpus":[0]}]` —— 前端「GPU 分配 → 组合」子菜单直接渲染这个。

    候选**不是**由这里枚举卡的组合来的:那样会绕过 hardware.yaml 里的运维约束
    (本机 GPU 0 是显示卡,腾空前不该拿它跑 TP)。yaml 没声明多卡组 → 返回空 +
    `hint`,前端菜单就只剩单卡项(审查 #1)。异构 / 大小非 2 的幂的组会被丢掉。
    """
    from src.gpu.topology import candidate_groups

    groups = await asyncio.to_thread(candidate_groups)
    out: dict = {"groups": groups}
    if not groups:
        out["hint"] = (
            "configs/hardware.yaml 里没有声明多卡 group —— 张量并行的候选组只认那份"
            "拓扑(它记着'哪张卡在驱动显示器'这类运维约束)。要跨卡请先在其中加一个"
            "多卡 group(gpus + nvlink)。"
        )
    return out


@router.get("/scheduler/status")
async def scheduler_status(request: Request):
    """Return current model manager status."""
    model_mgr = request.app.state.model_manager
    return model_mgr.get_status()


# ── 物理删除(spec 2026-07-28-model-physical-delete)──────────────────────
#
# 两步式:先 /delete/preflight 拿「将删什么、将释放多少、谁在挡」,前端据此渲染确认框;
# 再 /delete 真删。**执行端点服务端重跑一遍预检**,不信任前端除 force 外的任何输入。
#
# 用 POST 而非 DELETE /{name}:组件条目名形如
# `component:diffusion_models:/abs/path/x.safetensors`,含 `/`,做不了 path 参数。


async def _delete_preflight(name: str, request: Request, session: AsyncSession) -> dict:
    """删除前的全量体检。抛 DeleteError → 路由转 HTTPException。"""
    configs = scan_models()
    target = md.resolve_target(name, configs)
    md.assert_safe_target(target)

    # 硬 blocker:还在显存里(问 ModelManager)。超分/组件/LoRA 文件自 2026-09-26 起
    # 不再有任何进程加载(自建图像引擎已删),没有「还在显存里」这一说。
    loaded: dict | None = None
    if target.kind == "model":
        if _is_engine_loaded(name, request):
            loaded = {"status": "loaded", "gpu": _get_loaded_gpu(name, request)}
        elif _loading_states.get(name, {}).get("status") == "loading":
            loaded = {"status": "loading", "gpu": None}

    services = (
        await md.find_referencing_services(session, target.engine_key)
        if target.engine_key
        else []
    )

    yaml_path = None
    meta_row = False
    override_rows = 0
    if target.engine_key:
        candidate = md.models_d_dir() / f"{target.engine_key}.yaml"
        yaml_path = str(candidate) if candidate.is_file() else None
        meta_row, override_rows = await md.count_registry_rows(session, target.engine_key)

    terms = [target.engine_key, target.local_path, target.path.name]
    code = md.scan_code_refs([t for t in terms if t])

    return {
        "name": name,
        "kind": target.kind,
        "target_path": str(target.path),
        "is_dir": target.is_dir,
        "size_bytes": md.path_size_bytes(target.path),
        "blockers": {"loaded": loaded, "services": services},
        "registry_cleanup": {
            "models_d_yaml": yaml_path,
            "model_metadata": meta_row,
            "runtime_overrides": override_rows,
        },
        "code_refs": code["refs"],
        "code_refs_truncated": code["truncated"],
        "code_refs_error": code["scan_error"],
        "_target": target,
    }


@router.post("/delete/preflight", dependencies=[Depends(require_admin)])
async def delete_preflight(
    request: Request,
    body: dict = Body(...),
    session: AsyncSession = Depends(get_async_session),
):
    """删除前体检:目标路径 / 将释放空间 / 阻断项 / 注册表清理清单 / 源码残留引用。"""
    name = (body or {}).get("name")
    if not name:
        raise HTTPException(400, "name 必填")
    try:
        out = await _delete_preflight(name, request, session)
    except md.DeleteError as e:
        raise HTTPException(e.status_code, e.detail) from e
    out.pop("_target", None)
    return out


@router.post("/delete", dependencies=[Depends(require_admin)])
async def delete_engine(
    request: Request,
    body: dict = Body(...),
    session: AsyncSession = Depends(get_async_session),
):
    """物理删除:rm -rf 磁盘 → 清注册表 → 清缓存 → 回残留源码引用报告。不可撤销。"""
    name = (body or {}).get("name")
    if not name:
        raise HTTPException(400, "name 必填")
    force = bool((body or {}).get("force"))

    try:
        pre = await _delete_preflight(name, request, session)
    except md.DeleteError as e:
        raise HTTPException(e.status_code, e.detail) from e

    if pre["blockers"]["loaded"]:
        raise HTTPException(
            409, f"{name} 正在显存中({pre['blockers']['loaded']['status']}),请先卸载再删除"
        )
    if pre["blockers"]["services"] and not force:
        names = ", ".join(s["name"] for s in pre["blockers"]["services"])
        raise HTTPException(
            409, f"{name} 被这些服务引用: {names}。确认后加 force=true 强制删除"
        )

    target = pre["_target"]
    freed, disk_errors = md.delete_disk(target)

    cleaned = {"models_d_yaml": False, "model_metadata": False, "runtime_overrides": 0}
    if target.engine_key:
        try:
            cleaned["models_d_yaml"] = md.delete_models_d_yaml(target.engine_key)
        except md.DeleteError:
            logger.exception("delete: models.d yaml 清理失败 key=%s", target.engine_key)
        db_out = await md.clean_registry_db(session, target.engine_key)
        cleaned["model_metadata"] = db_out["model_metadata"]
        cleaned["runtime_overrides"] = db_out["runtime_overrides"]
        mgr = _get_model_manager(request)
        if mgr is not None and getattr(mgr, "_registry", None) is not None:
            mgr._registry.reload()

    md.invalidate_all_caches()

    # 残留扫描跑在删除前 → models.d/<key>.yaml 会把自己扫进去,但它刚被删掉。
    # 剔除它,别让报告指向一个已不存在的文件。
    code_refs = md.drop_refs_to(
        pre["code_refs"],
        pre["registry_cleanup"]["models_d_yaml"] if cleaned["models_d_yaml"] else None,
    )

    logger.warning(
        "engine deleted: name=%s path=%s freed=%dB registry=%s disk_errors=%d",
        name, target.path, freed, cleaned, len(disk_errors),
    )
    return {
        "deleted": True,
        "name": name,
        "target_path": str(target.path),
        "freed_bytes": freed,
        "disk_errors": disk_errors,
        "registry_cleaned": cleaned,
        "code_refs": code_refs,
        "code_refs_truncated": pre["code_refs_truncated"],
        "code_refs_error": pre["code_refs_error"],
    }
