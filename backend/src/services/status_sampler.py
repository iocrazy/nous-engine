"""状态页采样器 + 聚合(2026-06-17,status 页 v1)。

- compute_statuses():现算各组件当前状态(端点实时用,不读 DB)。
- status_sampler_loop():后台每 60s 把当前状态落 status_samples,定期清 8 天外旧行。
- uptime_history():从 status_samples 聚合「过去 N 天每组件每天 uptime%」给 status 页画条。

组件故意**不含** Lane-K llm runner supervisor —— 它常驻 running:false 但 vLLM 由
model_manager 独立 spawn、服务正常,纳入会恒 degraded(噪声,2026-06-16 巡检脚本同理)。
LLM 组件 = 直接看 llm 型 vLLM 实例健康。
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.status_sample import StatusSample

logger = logging.getLogger(__name__)

OPERATIONAL = "operational"
DEGRADED = "degraded"
DOWN = "down"
IDLE = "idle"  # 基础设施在线但没加载模型(不是故障,只是没东西可服务)
# worst 排序(算 overall 用):idle 视同 operational —— 某子系统没装模型不该把整体标成
# 降级/异常。只有 degraded/down 才下拉 overall。idle 只是**单组件**的展示态。
_RANK = {OPERATIONAL: 0, IDLE: 0, DEGRADED: 1, DOWN: 2}
# uptime 口径:idle 算"在线/可用"(没装模型不是宕机),不拉低可用率。
_UP = {OPERATIONAL, IDLE}

# (key, 显示名)。顺序即页面展示顺序。
COMPONENTS: list[tuple[str, str]] = [
    ("backend", "后端 API"),
    ("database", "数据库"),
    ("llm", "LLM 推理 (vLLM)"),
    ("embedding", "向量 (vLLM)"),
    ("image", "图像 Runner"),
    ("tts", "语音 Runner"),
    ("gpu", "GPU"),
    ("comfy", "ComfyUI 桥"),
]
COMPONENT_KEYS = [k for k, _ in COMPONENTS]
_COMFY_STATE = {"ok": OPERATIONAL, "degraded": DEGRADED, "down": DOWN}

DEFAULT_INTERVAL_S = 60.0
RETENTION_DAYS = 8
PRUNE_EVERY_S = 3600.0


def worst(statuses) -> str:
    """多个状态取最差(down > degraded > operational)。空 → operational。"""
    s = list(statuses)
    if not s:
        return OPERATIONAL
    return max(s, key=lambda x: _RANK.get(x, 0))


def _vllm_targets_by_type(model_manager):
    """已加载且有端口的 vLLM 实例,按 model_type 分组 → {type: [(mid, port)]}。"""
    out: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for mid, entry in getattr(model_manager, "_models", {}).items():
        adapter = getattr(entry, "adapter", None)
        if not getattr(adapter, "is_loaded", False):
            continue
        port = getattr(adapter, "_port", None) or getattr(adapter, "port", None)
        if not port:
            continue
        mtype = getattr(getattr(entry, "spec", None), "model_type", None) or "llm"
        out[str(mtype)].append((mid, int(port)))
    return out


async def _vllm_component_status(targets) -> str:
    """一组 vLLM 实例(同类)的组件状态:无实例=**idle**(没装模型,不是故障也不是"运行正常");
    全健康=operational;任一连不上=down。"""
    if not targets:
        return IDLE
    from src.services.vllm_metrics import snapshot_all
    snaps = await snapshot_all(targets)
    if any(not s.get("healthy") for s in snaps):
        return DOWN
    return OPERATIONAL


async def compute_statuses(
    app_state,
    session: AsyncSession | None = None,
    details: dict[str, str] | None = None,
) -> dict[str, str]:
    """现算每个组件当前状态。每项独立 try —— 单项探测失败记 down,绝不让采样/端点崩。

    传了 `details` 就往里填「组件 → 一句话原因」(目前只有 comfy 的 problems),给状态页
    在组件名下显示;采样落库不需要,不传。
    """
    out: dict[str, str] = {}

    # backend:能跑到这就是活的。
    out["backend"] = OPERATIONAL

    # database:SELECT 1。
    try:
        if session is not None:
            await session.execute(text("SELECT 1"))
            out["database"] = OPERATIONAL
        else:
            from src.models.database import get_session_factory
            async with get_session_factory()() as s:
                await s.execute(text("SELECT 1"))
            out["database"] = OPERATIONAL
    except Exception as e:  # noqa: BLE001
        logger.warning("status: database check failed: %s", e)
        out["database"] = DOWN

    mgr = getattr(app_state, "model_manager", None)
    by_type = _vllm_targets_by_type(mgr) if mgr else {}

    # llm / embedding:对应 type 的 vLLM 实例健康。
    try:
        out["llm"] = await _vllm_component_status(by_type.get("llm", []))
    except Exception as e:  # noqa: BLE001
        logger.warning("status: llm check failed: %s", e)
        out["llm"] = DOWN
    try:
        out["embedding"] = await _vllm_component_status(by_type.get("embedding", []))
    except Exception as e:  # noqa: BLE001
        logger.warning("status: embedding check failed: %s", e)
        out["embedding"] = DOWN

    # image / tts runner:三态 —— 进程死=down;进程活但没加载模型=**idle**(用户反馈:
    # 没装 TTS 不该显示"运行正常");进程活+有已加载 adapter=operational。runner 上报的
    # 已加载 adapter 在 supervisor.loaded_models(ping/pong 快照),主进程据此判 idle vs serving。
    runners: dict[str, dict] = {}
    for sup in getattr(app_state, "runner_supervisors", []) or []:
        try:
            runners[sup.group_id] = {
                "running": bool(sup.is_running),
                "n_loaded": len(getattr(sup, "loaded_models", []) or []),
            }
        except Exception:  # noqa: BLE001
            pass
    for key in ("image", "tts"):
        r = runners.get(key)
        if r is None or not r["running"]:
            out[key] = DOWN  # runner 进程不在/死了
        elif r["n_loaded"] > 0:
            out[key] = OPERATIONAL
        else:
            out[key] = IDLE  # 进程在、随时可加载,但当前没装模型

    # gpu:nvidia-smi 能列出卡。
    try:
        from src.services.gpu_monitor import get_gpu_stats
        # get_gpu_stats → subprocess(nvidia-smi, timeout=5) 同步阻塞;compute_statuses
        # 被状态页每 15s + sampler 每 60s 调,均 ≥ gpu_monitor 的 2s 缓存 → 必然重跑
        # subprocess → 丢线程池,别卡事件循环最长 5s(性能二轮 P1-A)。
        gpu_stats = await asyncio.to_thread(get_gpu_stats)
        out["gpu"] = OPERATIONAL if len(gpu_stats) > 0 else DOWN
    except Exception as e:  # noqa: BLE001
        logger.warning("status: gpu check failed: %s", e)
        out["gpu"] = DOWN

    # comfy:sidecar 体检(在线 + systemd 身份 + 监听地址),见 comfy/sidecar_status.py。
    try:
        from src.services.comfy import sidecar_status as comfy_sidecar
        comfy = await comfy_sidecar.sidecar_status()
        out["comfy"] = _COMFY_STATE.get(comfy.get("state"), DOWN)
        if details is not None and comfy.get("problems"):
            details["comfy"] = ";".join(comfy["problems"])
    except Exception as e:  # noqa: BLE001
        logger.warning("status: comfy check failed: %s", e)
        out["comfy"] = DOWN

    return out


async def sample_once(app_state, session: AsyncSession) -> None:
    statuses = await compute_statuses(app_state, session)
    now = datetime.now(timezone.utc)
    session.add_all([
        StatusSample(component=k, status=v, ts=now) for k, v in statuses.items()
    ])
    await session.commit()


async def _prune(session: AsyncSession) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    await session.execute(delete(StatusSample).where(StatusSample.ts < cutoff))
    await session.commit()


async def status_sampler_loop(app_state, *, interval_s: float = DEFAULT_INTERVAL_S) -> None:
    """后台:每 interval_s 采样一次;每 PRUNE_EVERY_S 清一次旧行。loop 永不因单轮异常退出。"""
    from src.models.database import get_session_factory
    last_prune = 0.0
    elapsed = 0.0
    while True:
        await asyncio.sleep(interval_s)
        elapsed += interval_s
        try:
            async with get_session_factory()() as session:
                await sample_once(app_state, session)
                if elapsed - last_prune >= PRUNE_EVERY_S:
                    await _prune(session)
                    last_prune = elapsed
        except Exception as e:  # noqa: BLE001
            logger.warning("status sampler loop error: %s", e)


async def uptime_history(session: AsyncSession, *, days: int = 7) -> dict[str, dict]:
    """聚合每组件「过去 days 天每天 uptime%」+ 总 uptime%。

    返回 {component: {"uptime_pct": float, "days": [{"date","uptime_pct","status","samples"}]}}。
    用 func.date(ts) 按 UTC 日分桶,桶内 uptime=operational 占比。
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day_col = func.date(StatusSample.ts)
    # uptime = "可用"占比;idle(在线没装模型)算可用,不算宕机(否则没常驻模型的组件
    # uptime 永远 ~0% 误报)。只有 down 拉低可用率。
    ok_col = func.sum(case((StatusSample.status.in_(list(_UP)), 1), else_=0))
    rows = (await session.execute(
        select(StatusSample.component, day_col.label("d"),
               func.count().label("total"), ok_col.label("ok"))
        .where(StatusSample.ts >= since)
        .group_by(StatusSample.component, day_col)
    )).all()

    # {component: {date_str: (ok, total)}}
    acc: dict[str, dict[str, tuple[int, int]]] = defaultdict(dict)
    for comp, d, total, ok in rows:
        acc[comp][str(d)] = (int(ok or 0), int(total or 0))

    today = datetime.now(timezone.utc).date()
    date_keys = [str(today - timedelta(days=days - 1 - i)) for i in range(days)]

    result: dict[str, dict] = {}
    for comp in COMPONENT_KEYS:
        per_day = acc.get(comp, {})
        day_list = []
        tot_ok = tot_all = 0
        for dk in date_keys:
            ok, total = per_day.get(dk, (0, 0))
            tot_ok += ok
            tot_all += total
            if total == 0:
                day_list.append({"date": dk, "uptime_pct": None, "status": "nodata", "samples": 0})
            else:
                pct = round(100.0 * ok / total, 2)
                # 当天有任一非 operational 即标 degraded/down(画条用):全绿 operational,
                # 否则按最差。简单用 pct 阈值:100=operational,>0=degraded,0=down。
                st = OPERATIONAL if pct >= 100 else (DOWN if pct == 0 else DEGRADED)
                day_list.append({"date": dk, "uptime_pct": pct, "status": st, "samples": total})
        overall = round(100.0 * tot_ok / tot_all, 2) if tot_all else None
        result[comp] = {"uptime_pct": overall, "days": day_list}
    return result
