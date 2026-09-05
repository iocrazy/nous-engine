import asyncio
import logging
import os

# torch 默认 CUDA_DEVICE_ORDER=FASTEST_FIRST,把最快的卡(Pro 6000)排到 cuda:0,
# 跟 nvidia-smi(PCI 顺序)+ hardware.yaml(按 nvidia-smi 写)错位 ——
# ModelManager.get_best_gpu() 用 nvidia-smi poll 取 PCI index,喂给 torch
# 当 cuda:N 就装错卡(实测 flux2 想去 Pro 6000 → 装到 3090)。
# setdefault 在 import torch 之前固定 PCI_BUS_ID,让三个索引系统一致;
# 用户 .env 同名变量优先(setdefault 不覆盖)。
os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.api.routes import understand, generate, tts, engines, audio, voices, openai_compat, ollama_compat, api_gateway as api_gateway_routes, settings, workflows, agents, skills, monitor, node_packages, execution_tasks, apps, logs, context_cache as context_cache_routes, files as files_routes, services as services_routes, workflow_publish as workflow_publish_routes, usage as usage_routes, dashboard as dashboard_routes, api_keys as api_keys_routes, anthropic_compat, observability, loras as loras_routes, image_files as image_files_routes, models as models_routes, predictions as predictions_routes, comfy_templates as comfy_templates_routes
from src.api.ws_tts import handle_tts_websocket
from src.services.gpu_monitor import memory_guard_loop
# WS 广播基础设施已下沉到 services/ws_hub(打破 services→api 反向依赖)。
from src.services.ws_hub import _ws_connections, ws_manager  # noqa: F401

logger = logging.getLogger(__name__)


def _make_component_event_handler(registry, ws):
    """Build the sync callback RunnerClient.on_component_event uses: update the
    backend mirror + fan out a WS push. WS broadcast is async → scheduled."""
    def _handler(evt) -> None:
        # round6:registry.update 早先在 try 外,抛异常会逃进 RunnerClient demux loop 杀掉它
        # (后续 run_node 全挂 5min)。client 侧已加回调守卫,这里再各自 try 兜底纵深。
        try:
            registry.update(evt.component_key, evt.state, evt.error)
        except Exception:  # noqa: BLE001
            logger.exception("component registry.update failed (%s)", evt.component_key)
        try:
            asyncio.get_running_loop().create_task(
                ws.broadcast_component_state(evt.component_key, evt.state, evt.error))
        except RuntimeError:
            pass  # no running loop — registry still updated
    return _handler


# 开发期微迁移(无 alembic):create_all 不给**已存在**的表加列/索引,这些幂等 DDL 补上
# 缺口(Postgres `IF NOT EXISTS`)。每条 best-effort —— 单条失败只 warn、不阻断启动。
# 提成模块常量:一处集中、可 grep、也是未来引入 alembic 时 baseline 的清单来源。
_MICRO_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE execution_tasks ADD COLUMN IF NOT EXISTS input_json JSONB",
    # 归属列(IDOR 防护):predictions by-id 端点据 api_key_id 校验 owner。
    "ALTER TABLE execution_tasks ADD COLUMN IF NOT EXISTS api_key_id BIGINT",
    "ALTER TABLE execution_tasks ADD COLUMN IF NOT EXISTS webhook_url VARCHAR(500)",
    "ALTER TABLE execution_tasks ADD COLUMN IF NOT EXISTS webhook_events JSONB",
    # PR-5b:files 作用域 instance_id → api_key_id。加列 + 旧列降 nullable(孤儿)+ 新键/索引。
    "ALTER TABLE files ADD COLUMN IF NOT EXISTS api_key_id BIGINT",
    "ALTER TABLE files ALTER COLUMN instance_id DROP NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_files_apikey_sha256 ON files (api_key_id, sha256)",
    "CREATE INDEX IF NOT EXISTS ix_files_apikey_created ON files (api_key_id, created_at)",
    # legacy rip:memory_entries 作用域 instance_id → api_key_id。降 instance_id nullable
    # (M:N 无单一 instance)+ 建 api_key 索引(旧 idx_mem_inst_* create_all 已建,不删)。
    "ALTER TABLE memory_entries ALTER COLUMN instance_id DROP NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_mem_key_created ON memory_entries (api_key_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_mem_key_ctx_cat ON memory_entries (api_key_id, context_key, category)",
    # 节点分组(ComfyUI 式可视框,不入执行图)。旧表补列,默认空 []。
    "ALTER TABLE workflows ADD COLUMN IF NOT EXISTS groups JSONB DEFAULT '[]'::jsonb",
    # execution_tasks.created_at 索引 —— 支撑 usage 保留清理按 created_at 删旧行
    # (usage_retention);既有 prod 表补索引(新/测试 DB 由模型 __table_args__ 建)。
    "CREATE INDEX IF NOT EXISTS ix_execution_tasks_created ON execution_tasks (created_at)",
    # prod 漂移 reconcile(alembic stamp 时 check 出,2026-07-06):models 早把 agent_id
    # 加宽到 String(128)+index、key_prefix 加 index,但既有 prod 列仍 VARCHAR(64)/缺索引
    # (micro-migration 历史只加列不改类型/补这些索引)。加宽是非破坏(64→128 不丢数据);
    # 索引 IF NOT EXISTS 幂等。每条都是 best-effort:fresh DB 由 create_all 直接按 model
    # 建对,这些语句在它上面是 no-op/已存在,失败也无害。
    "ALTER TABLE llm_usage ALTER COLUMN agent_id TYPE VARCHAR(128)",
    "ALTER TABLE response_sessions ALTER COLUMN agent_id TYPE VARCHAR(128)",
    "CREATE INDEX IF NOT EXISTS ix_llm_usage_agent_id ON llm_usage (agent_id)",
    "CREATE INDEX IF NOT EXISTS ix_response_sessions_agent_id ON response_sessions (agent_id)",
    "CREATE INDEX IF NOT EXISTS ix_instance_api_keys_key_prefix ON instance_api_keys (key_prefix)",
    # 服务级「开机启动」(2026-09-03)。alembic 有对应迁移
    # (c5d2e9b74a10),但生产启动仍走 create_all —— create_all 不给**已存在**的表加列,
    # 没这条的话线上重启后 service_instances 查询全炸 UndefinedColumn。幂等,可共存。
    "ALTER TABLE service_instances ADD COLUMN IF NOT EXISTS autostart BOOLEAN NOT NULL DEFAULT false",
    # 模型级 GPU 组 / 张量并行(2026-09-03)。alembic 有对应迁移(d7a4b1e6c093),
    # 但生产启动仍走 create_all —— create_all 不给**已存在**的表加列,没这条的话
    # 线上重启后 model_runtime_overrides 查询全炸 UndefinedColumn。幂等,可共存。
    "ALTER TABLE model_runtime_overrides ADD COLUMN IF NOT EXISTS gpus JSONB",
)


async def _connect_and_init_db() -> None:
    """建表 + 跑微迁移,带连接重试。lifespan 启动第一步(god-lifespan 拆分,行为不变)。

    Retry DB connect:docker postgres 容器可能 backend 启动时还在 healthcheck 阶段
    (race condition seen 2026-05-07)。Backoff 2/4/8/16/32/60s = 122s 总等超时再死。
    """
    from src.models.database import Base, create_engine
    import src.models.voice_preset  # noqa: F401
    import src.models.tts_usage  # noqa: F401
    import src.models.service_instance  # noqa: F401
    import src.models.instance_api_key  # noqa: F401
    import src.models.model_metadata  # noqa: F401
    import src.models.workflow  # noqa: F401
    import src.models.execution_task  # noqa: F401
    import src.models.llm_usage  # noqa: F401
    import src.models.context_cache  # noqa: F401
    import src.models.response_session  # noqa: F401
    import src.models.memory  # noqa: F401
    import src.models.api_gateway  # noqa: F401
    import src.models.admin_credentials  # noqa: F401
    import src.models.log_entry  # noqa: F401  # structured logs live in main DB now
    import src.models.status_sample  # noqa: F401  # status 页 7 天 uptime 采样
    import src.models.comfy_template  # noqa: F401  # register model

    # 安全 review P2:生产(admin gate 开)禁止用可猜测的默认 DB 口令 mindcenter:mindcenter。
    # 只在 ADMIN_PASSWORD 非空时拦(tests/dev 把它设空 → 不受影响,且各自 override DATABASE_URL)。
    from src.config import get_settings as _get_settings
    _s = _get_settings()
    if _s.ADMIN_PASSWORD and "mindcenter:mindcenter@" in _s.DATABASE_URL:
        raise RuntimeError(
            "拒绝以默认弱口令 DB(mindcenter:mindcenter)启动生产实例。"
            "请在 backend/.env 设置强口令的 DATABASE_URL。"
        )

    last_err = None
    for attempt in range(6):
        # round4 #2:engine 用 try/finally dispose —— begin() 在 postgres 还没起来时会抛,
        # 早先 dispose 在 success 之后,失败路径跳过它 → 每次重试泄漏一个连接池。
        engine = create_engine()
        try:
            # 确保新表登记进 Base.metadata,create_all 才会建(无 alembic;数据加载统一 2026-06-16)。
            import src.models.model_runtime_override  # noqa: F401, PLC0415
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                from sqlalchemy import text  # noqa: PLC0415
                for _ddl in _MICRO_MIGRATIONS:
                    try:
                        await conn.execute(text(_ddl))
                    except Exception as _e:  # noqa: BLE001 — 微迁移 best-effort
                        logger.warning("micro-migration skipped (%s): %s", _ddl, _e)
            logger.info("Database tables ensured%s", f" (after {attempt} retries)" if attempt else "")
            return
        except Exception as e:
            last_err = e
            wait_s = min(2 ** (attempt + 1), 60)
            logger.warning(
                "DB connect attempt %d failed: %s — retrying in %ds",
                attempt + 1, type(e).__name__, wait_s,
            )
            await asyncio.sleep(wait_s)
        finally:
            await engine.dispose()
    logger.error("DB connect failed after 6 retries; last error: %s", last_err)
    raise last_err


def _install_log_handlers() -> None:
    """装 structured-log writer + application-log handler(god-lifespan 拆分,行为不变)。"""
    # Start the structured-log writer: async queue + single batch-insert consumer
    # into the main PG DB (spec 2026-06-10 — one DB, no separate SQLite log_db).
    from src.services.log_store import log_writer
    log_writer.start()
    logger.info("Log writer started (PG-backed)")

    from src.services.log_collector import DbLogHandler
    db_handler = DbLogHandler()
    db_handler.setLevel(logging.INFO)
    # Surface application INFO logs to stdout too — otherwise operators tailing
    # uvicorn only see the access log, and slow image-load helpers (which print
    # "image: dequant fp8→bf16 done ..." progress markers) look like a black
    # hole from the terminal. Without these handlers the lines went only to
    # the DB log handler, which is queryable but invisible to humans.
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(name)s — %(message)s")
    )
    for _logger_name in ("src", "nous"):
        _l = logging.getLogger(_logger_name)
        _l.setLevel(logging.INFO)
        _l.addHandler(db_handler)
        _l.addHandler(stream_handler)
    logger.info("Application log collector installed (db + stdout)")


def _start_background_tasks(app, model_mgr):
    """启动常驻后台 loop(god-lifespan 拆分第二刀)。

    内联 async def 保持在函数内、闭包 app/model_mgr —— 不把闭包变量穿线到
    模块级,零 NameError 风险。返回 shutdown/finally 要 cancel 的 4 个句柄;
    NOUS_DISABLE_BG_TASKS=1(测试)时全为空([]/None)。
    """
    # NOUS_DISABLE_BG_TASKS=1 → skip all background tasks.
    # CRITICAL for tests: the default background set includes memory_guard_loop
    # which polls `nvidia-smi` via subprocess every 5s. When multiple test
    # processes simultaneously trigger lifespan (via TestClient), concurrent
    # nvidia-smi invocations contend with gnome-shell's GPU compositor, which
    # can crash the NVIDIA driver and log out the X session. conftest.py sets
    # this env var at the top so `uv run pytest` never spawns these loops.
    import os as _os
    _bg_tasks_disabled = _os.getenv("NOUS_DISABLE_BG_TASKS") == "1"

    cache_cleanup_task = None
    response_cleanup_task = None
    partial_worker = None
    # round4 #8/#9:常驻后台 loop 早先用裸 asyncio.create_task,既不持引用(Py3.11+
    # event loop 只持弱引用 → 起手无 sleep 护栏的任务可能被 GC 丢弃),
    # shutdown 也不 cancel(留半完成 subprocess)。收进 list 持引用 + finally 统一 cancel。
    bg_tasks: list = []

    if not _bg_tasks_disabled:
        # 开机预加载**只有一条后台任务**(_preload_sequence),顺序做两件事:
        #   ① resident 模型(preload_residents)
        #   ② autostart=true 服务引用到的模型(service_autostart)
        # 没有第三条 —— 尤其**没有**「按已发布工作流的模型依赖预加载」(2026-09-03 删的
        # _load_wf_deps:把每个 published 工作流引用到的模型开机全 load_model,完全不看
        # resident 标记,用户没开「自动加载」的模型照样开机占满显存)。工作流真正执行时
        # runner 走 get_or_load 按需加载。
        #
        # 两步**顺序 await 在同一个 task 里**,不另起并发 task:2026-07-06 生产事故就是两个
        # 独立 task 同一瞬间往同一张卡 spawn 两个 vLLM(见 model_manager._global_load_lock)。
        #
        # Resident models marked resident: preload in the background, ordered
        # by preload_order ascending (spec 4.2). The ~120s diffusers compose
        # must not block /health (cloudflared / systemd probes would mark the
        # backend down). preload_residents is fail-soft: a single model's
        # OOM / corrupt-weights failure records into mm._load_failures and is
        # surfaced on /health — it never blocks startup or the rest of the
        # preload sequence (spec 4.3). on_loaded flips the engines/models
        # cache + UI badge within ~1s per successful load.
        async def _on_resident_loaded(spec_id: str) -> None:
            from src.api.response_cache import invalidate as _invalidate
            _invalidate("models", "engines")
            from src.api.websocket import ws_manager as _ws
            await _ws.broadcast_model_status(spec_id, "loaded")

        async def _preload_sequence() -> None:
            await model_mgr.preload_residents(on_loaded=_on_resident_loaded)
            # autostart 服务的模型跟在 resident 后面(fail-soft,自己吞异常)。
            from src.models.database import get_session_factory as _asf
            from src.services.service_autostart import preload_autostart_services
            await preload_autostart_services(
                _asf(), model_mgr, on_loaded=_on_resident_loaded,
            )

        # Persist the task ref so 3.11+ doesn't garbage-collect a still-running
        # background coroutine and silently drop the preload.
        app.state._resident_preload_task = asyncio.create_task(_preload_sequence())

        # Start idle model checker background task
        async def idle_checker():
            while True:
                await asyncio.sleep(60)
                try:
                    await model_mgr.check_idle_models()
                except Exception as e:
                    logger.warning("Idle model check failed: %s", e)

        bg_tasks.append(asyncio.create_task(idle_checker()))
        bg_tasks.append(asyncio.create_task(memory_guard_loop(model_mgr, reserved_gb=4.0)))
        # GPU 热保护看门狗:超温告警 + 自动降载(卸非常驻/非在用模型救卡)。活机实测
        # 加载一下就 86°C/风扇 0%、离降频仅 ~7°C —— 满载推理该有软件刹车。
        from src.services.gpu_thermal_guard import gpu_thermal_guard_loop
        bg_tasks.append(asyncio.create_task(gpu_thermal_guard_loop(model_mgr)))

        # vLLM 健康看门狗(稳定性加固 2026-06-16):自愈「model_manager 记着 loaded 但
        # vLLM 端口连不上」的陈旧/孤儿态(改 cap·重启周期 / host OOM 杀子进程后的
        # ConnectError,2026-06-16 qwen35 真机踩到)。只对 ConnectError + 连续确认动手。
        if _os.getenv("NOUS_DISABLE_VLLM_WATCHDOG") != "1":
            from src.services.vllm_watchdog import vllm_health_watchdog

            async def _wd_notify(mid: str, action: str) -> None:
                from src.api.response_cache import invalidate as _invalidate
                _invalidate("models", "engines")
                from src.api.websocket import ws_manager as _ws
                await _ws.broadcast_model_status(
                    mid, "loaded" if action == "reloaded" else "unloaded")

            bg_tasks.append(asyncio.create_task(
                vllm_health_watchdog(model_mgr, notify=_wd_notify)))

        # 状态页采样器(status 页 v1,2026-06-17):每 60s 给各组件落一行状态,供
        # status 页画 7 天 uptime 条。NOUS_DISABLE_STATUS_SAMPLER=1 可关。
        if _os.getenv("NOUS_DISABLE_STATUS_SAMPLER") != "1":
            from src.services.status_sampler import status_sampler_loop
            bg_tasks.append(asyncio.create_task(status_sampler_loop(app.state)))

        async def log_cleanup_loop():
            while True:
                await asyncio.sleep(3600)  # Every hour
                try:
                    from src.services.log_store import cleanup_logs
                    from src.models.database import get_session_factory
                    # Now async on the main DB; no to_thread needed (it awaits I/O,
                    # doesn't block the loop). One short-lived session per sweep.
                    async with get_session_factory()() as session:
                        await cleanup_logs(session)
                except Exception as e:
                    logger.warning("Log cleanup failed: %s", e)

        bg_tasks.append(asyncio.create_task(log_cleanup_loop()))

        async def usage_retention_loop():
            # usage/task 历史表保留清理(审查 P1:三表无限增长)。每 6h 删 >90 天旧行。
            while True:
                await asyncio.sleep(6 * 3600)
                try:
                    from src.models.database import get_session_factory
                    from src.services.usage_retention import cleanup_usage
                    async with get_session_factory()() as session:
                        await cleanup_usage(session)
                except Exception as e:  # noqa: BLE001
                    logger.warning("usage retention failed: %s", e)

        bg_tasks.append(asyncio.create_task(usage_retention_loop()))

        # Image output orphan reaper. PR-6's signed-URL TTL is 1h by
        # default; once a URL expires the file is unreachable but stays
        # on disk. Walk every 6h and delete files older than 24h
        # (4× the URL TTL leaves enough room for a caller who fetched
        # the URL near expiry to still pull the bytes once).
        async def image_orphan_reap_loop(interval_seconds: int = 6 * 3600):
            from src.api.routes.execution_tasks import collect_referenced_image_uuids
            from src.models.database import get_session_factory as _isf
            from src.services.image_output_storage import reap_orphans
            sf = _isf()
            while True:
                try:
                    # 图寿命=任务寿命(spec 2026-06-09 run-history):先查仍被 ExecutionTask
                    # 引用的图 uuid,只清没人引用的真 orphan(失败/已删任务残留)→ /history
                    # 画廊历史图不被误删。round4 #6:reap 同步全盘遍历,丢 to_thread 不卡 loop。
                    async with sf() as session:
                        keep = await collect_referenced_image_uuids(session)
                    await asyncio.to_thread(
                        reap_orphans, older_than_seconds=24 * 3600, keep_uuids=keep,
                    )
                except Exception:
                    logger.exception("image orphan reap error")
                try:
                    await asyncio.sleep(interval_seconds)
                except asyncio.CancelledError:
                    break

        bg_tasks.append(asyncio.create_task(image_orphan_reap_loop()))

        async def context_cache_cleanup_loop(interval_seconds: int = 3600):
            from src.services.context_cache_service import cleanup_expired
            from src.models.database import get_session_factory as _csf
            sf = _csf()
            while True:
                try:
                    async with sf() as s:
                        n = await cleanup_expired(s)
                        if n:
                            logger.info("context cache cleanup: %d expired rows", n)
                except Exception:
                    logger.exception("context cache cleanup error")
                try:
                    await asyncio.sleep(interval_seconds)
                except asyncio.CancelledError:
                    break

        cache_cleanup_task = asyncio.create_task(context_cache_cleanup_loop())

        # Step 4: expired-session cleanup + partial-write background worker
        async def response_cleanup_loop(interval_seconds: int = 3600):
            from src.services.responses_service import cleanup_expired_sessions
            from src.models.database import get_session_factory as _csf
            sf = _csf()
            while True:
                try:
                    async with sf() as s:
                        n = await cleanup_expired_sessions(s)
                        if n:
                            logger.info("response cleanup: %d expired sessions", n)
                except Exception:
                    logger.exception("response cleanup error")
                try:
                    await asyncio.sleep(interval_seconds)
                except asyncio.CancelledError:
                    break

        response_cleanup_task = asyncio.create_task(response_cleanup_loop())

        from src.api.routes import responses as responses_routes
        responses_routes._set_queue(asyncio.Queue(maxsize=1000))
        partial_worker = asyncio.create_task(responses_routes.partial_write_worker())
    return bg_tasks, cache_cleanup_task, response_cleanup_task, partial_worker


async def _seed_voice_preset_templates(sf) -> None:
    """首次启动(无模板时)把 voice_presets 迁成 workflow 模板(god-lifespan 拆分,行为不变)。"""
    from src.models.voice_preset import VoicePreset
    from src.models.workflow import Workflow as WfModel
    from sqlalchemy import select, func as sa_func

    async with sf() as session:
        wf_count = await session.scalar(
            select(sa_func.count()).select_from(WfModel).where(WfModel.is_template == True)  # noqa: E712
        )
        if wf_count == 0:
            result = await session.execute(select(VoicePreset))
            presets = result.scalars().all()
            for preset in presets:
                wf = WfModel(
                    name=preset.name,
                    description=f"从预设 '{preset.name}' 自动迁移",
                    is_template=True,
                    nodes=[
                        {"id": "n1", "type": "text_input", "data": {"text": ""}, "position": {"x": 0, "y": 0}},
                        {"id": "n2", "type": "tts_engine", "data": {
                            "engine": preset.engine,
                            **(preset.params or {}),
                        }, "position": {"x": 350, "y": 0}},
                        {"id": "n3", "type": "output", "data": {}, "position": {"x": 700, "y": 0}},
                    ],
                    edges=[
                        {"id": "e1", "source": "n1", "sourceHandle": "text", "target": "n2", "targetHandle": "text"},
                        {"id": "e2", "source": "n2", "sourceHandle": "audio", "target": "n3", "targetHandle": "audio"},
                    ],
                )
                session.add(wf)
            await session.commit()
            if presets:
                logger.info("Migrated %d voice presets to workflow templates", len(presets))


def _classify_orphan(healthy: bool, matched_spec: bool) -> str:
    """开机扫到的 vLLM 孤儿该怎么处置 —— "adopt" | "kill" | "kill_unmatched"。

    2026-09-05:此前只有「不健康 → 杀」「健康且能对上 spec → 接管」两条,健康但
    **对不上任何 spec** 的那条是**默默放着不管**。3.6 退役后这条就有牙了:后端崩溃重启
    (不是 `systemctl restart` —— 那会连子进程一起收)会留着 3.6 抱着 ~40G 在 GPU 0/2,
    3.8 的常驻预加载过不了 `_assert_explicit_fits`,之后每个 LLM 请求都是 503,
    而日志里一句话都没有。目录之外的 vLLM 没有任何人会来接管它 → 一律 error + 收掉。
    """
    if not healthy:
        return "kill"
    return "adopt" if matched_spec else "kill_unmatched"


def _kill_orphan_vllm(pid: int) -> None:
    """按 PID 收掉一个 vLLM 孤儿(不健康的、或目录里已没有对应 spec 的)。

    safe_killpg 拒绝 pgid<=1(广播守卫)并在发信号前复核该 PID 仍是 vLLM
    (扫描→击杀之间 PID 可能已死并被回收给 sshd/mihomo)。只有在组杀因**非广播**
    理由被拒时才退回单 PID kill。绝不用 `pkill -f`。
    """
    import signal as _signal  # noqa: PLC0415
    from src.services.safe_signal import (  # noqa: PLC0415
        _proc_cmdline_contains, safe_kill, safe_killpg,
    )

    def _is_vllm(p: int) -> bool:
        return _proc_cmdline_contains(p, "vllm")

    try:
        if not safe_killpg(pid, _signal.SIGKILL, verify=_is_vllm):
            if _is_vllm(pid):
                safe_kill(pid, _signal.SIGKILL)
    except Exception as e:
        logger.warning("Failed to kill orphan pid=%d: %s", pid, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create database tables on startup."""
    # Ensure localhost requests bypass proxy
    no_proxy = os.environ.get("NO_PROXY", "")
    if "localhost" not in no_proxy:
        os.environ["NO_PROXY"] = f"{no_proxy},localhost,127.0.0.1" if no_proxy else "localhost,127.0.0.1"

    await _connect_and_init_db()
    _install_log_handlers()

    # GPU 拓扑预热(审查 #17):`nvidia-smi topo -m` / 卡信息各要几十毫秒的同步
    # subprocess。启动时在线程池里跑一次,之后放置决策与 /api/v1/gpu/groups 全是
    # 缓存命中,不会在请求路径或 load 路径上阻塞事件循环。失败不阻塞启动。
    from src.gpu.topology import warm_caches as _warm_gpu_topology
    await asyncio.to_thread(_warm_gpu_topology)

    # Auto-sync model metadata for any new engines
    from src.models.database import get_session_factory
    from src.services.model_metadata_service import sync_metadata

    # round4 #1/#2:共享 memoized 工厂(别每处新建 engine);round4 #2(config#1):sf 在
    # try 外取 —— get_session_factory() 只返回工厂、不碰 DB,放 try 外才不会在 sync_metadata
    # 失败被吞后让下面 152 行的 `async with sf()` 撞 NameError(早先 sf 在 try 内)。
    sf = get_session_factory()

    # 运行时模型覆盖(resident/gpu/vram_budget)从 DB hydrate 进进程缓存(数据加载统一 2026-06-16)。
    # **必须在 model_manager/registry 创建前**(它们 _load 同步读缓存)+ 预加载前。
    # 一次性迁移旧 runtime_overrides.json(表为空时)→ 不丢现有覆盖。失败非致命(回退空覆盖)。
    try:
        from src.config import _resolve_path, _RUNTIME_OVERRIDES_REL
        from src.services import runtime_override_store
        await runtime_override_store.migrate_json_if_empty(
            sf, str(_resolve_path(_RUNTIME_OVERRIDES_REL)))
        await runtime_override_store.hydrate(sf)
    except Exception as e:  # noqa: BLE001 — 覆盖 hydrate 失败不阻断启动(回退 yaml 默认)
        logger.warning("runtime override hydrate failed (non-fatal): %s", e)

    # PR-9(spec 2026-07-20-moss-asr §8):清理孤儿 running 的直连 ASR 任务 —— backend 崩溃/
    # 重启会留永久 running 的两段式 ASR task(同步转写无后台 worker 复活),一次性置 failed。
    # 防御式,非致命(函数内部已兜底)。
    try:
        from src.services.api_call_tasks import fail_orphaned_running_asr_tasks
        n_orphans = await fail_orphaned_running_asr_tasks()
        if n_orphans:
            logger.info("清理孤儿 running ASR 任务:%d 条置 failed", n_orphans)
    except Exception as e:  # noqa: BLE001 — 清理失败不阻断启动
        logger.warning("orphan ASR task cleanup failed (non-fatal): %s", e)

    # Wave 1 MemoryProvider: init PGMemoryProvider + expose via app.state
    from src.services.memory.pg_provider import PGMemoryProvider
    try:
        app.state.memory_provider = PGMemoryProvider(session_factory=sf)
        await app.state.memory_provider.initialize()
        logger.info("MemoryProvider initialized (pg)")
    except Exception as e:
        logger.warning("MemoryProvider init failed (non-fatal): %s", e)

    try:
        async with sf() as session:
            await sync_metadata(session)
        logger.info("Model metadata synced")
    except Exception as e:
        logger.warning("Model metadata sync failed (non-fatal): %s", e)

    # Auto-migrate voice presets to workflow templates
    await _seed_voice_preset_templates(sf)

    # Scan node packages
    from nodes import scan_packages
    scan_packages()
    logger.info("Node packages scanned")

    # Create ModelManager
    from src.services.inference.registry import ModelRegistry
    from src.services.gpu_allocator import GPUAllocator
    from src.services.model_manager import ModelManager

    config_path = str(Path(__file__).resolve().parent.parent.parent / "configs" / "models.yaml")
    registry = ModelRegistry(config_path)
    allocator = GPUAllocator()
    model_mgr = ModelManager(registry=registry, allocator=allocator)
    app.state.model_manager = model_mgr

    # 启动扫描 + 自检:暖组件下拉索引(loader 节点用)+ 每角色计数 + 整模型完整性。
    # Fail-soft — 扫描/自检出错不阻塞启动,降级到空索引。
    try:
        from src.services.component_scanner import get_component_index, selfcheck_report
        report = selfcheck_report(force_refresh=True)  # 扫一遍 + 填缓存
        app.state.component_index = get_component_index()
        _roles = ", ".join(f"{r}={n}" for r, n in report["counts"].items())
        logger.info("模型扫描自检:%s", _roles)
        for _w in report["warnings"]:
            logger.warning("模型扫描自检:%s", _w)
    except Exception:  # noqa: BLE001 — index is non-critical at boot
        logger.exception("模型扫描自检失败;serving empty index")
        app.state.component_index = {role: [] for role in ("diffusion_models", "clip", "vae", "loras", "checkpoint")}

    # Wire ModelManager into workflow executor
    from src.services.workflow_executor import set_model_manager
    set_model_manager(model_mgr)

    # ------------------------------------------------------------------
    # Lane K: lifespan wiring —— spawn RunnerSupervisor / LLMRunner per
    # hardware.yaml group + expose via app.state for /health, /runners,
    # and workflow dispatch (spec §4.1 / §4.2).
    #
    # V1.5 lanes (#95–#106) shipped RunnerSupervisor / LLMRunner /
    # RunnerClient as standalone classes but nobody instantiated them in
    # lifespan, so app.state.runner_supervisors was unset, /runners
    # returned [], and dispatch nodes hit "runner_client is None".
    #
    # Gate: NOUS_DISABLE_RUNNER_SPAWN=1 (or unset by default in tests via
    # conftest.NOUS_DISABLE_BG_TASKS) skips the spawn block entirely so the
    # existing test suite — which would otherwise multiprocessing.spawn real
    # subprocesses + try to load real models — stays fast. Production
    # systemd unit sets NOUS_DISABLE_RUNNER_SPAWN=0 explicitly.
    # ------------------------------------------------------------------
    runner_supervisors: list = []
    runner_clients: dict[str, object] = {}
    llm_runner = None
    # Spawn runners only when explicitly enabled. Production systemd unit sets
    # NOUS_DISABLE_RUNNER_SPAWN=0 ; tests / dev default = skip (= "1") so the
    # existing fast pytest suite is unaffected, and conftest.NOUS_DISABLE_BG_TASKS
    # cannot accidentally trigger real multiprocessing.spawn of runner subprocesses.
    import os as _lane_k_os
    _runner_spawn_enabled = _lane_k_os.getenv("NOUS_DISABLE_RUNNER_SPAWN", "1") == "0"
    if _runner_spawn_enabled:
        from src.runner.supervisor import RunnerSupervisor
        from src.runner.llm_runner import LLMRunner
        from src.runner.gpu_free_probe import make_gpu_free_probe

        gpu_probe = make_gpu_free_probe()
        models_yaml_path = config_path  # 同一份 models.yaml,runner 子进程也用它

        for group in allocator.groups():
            if group.role == "llm":
                # LLMRunner —— per spec §4.1 每个 role:llm group 一个。
                # 不在 lifespan 里 spawn vLLM —— vLLM 的实际启动走现有
                # preload_residents 路径（下面 _resident_preload_task）。
                # LLMRunner 这里只持有「将来要管的 adapter 引用」+ GPU 列表,
                # 让 crash-recovery / health probe / restart 接口可用。
                #
                # 选取该 group 的代表 model_key:首个 type=llm 的 spec。无 spec
                # → 仍构造一个空壳（adapter=None,model_key=空）—— 测试 / 早期
                # 部署不带 yaml 时也要让 /health 等读 app.state.llm_runner 不崩。
                llm_specs = [s for s in registry.specs if s.model_type == "llm"]
                rep_model_key = llm_specs[0].id if llm_specs else f"llm-{group.id}"
                rep_adapter = (
                    model_mgr.get_adapter(rep_model_key) if llm_specs else None
                )
                llm_runner = LLMRunner(
                    model_key=rep_model_key,
                    adapter=rep_adapter,
                    llm_gpus=list(group.gpus),
                    gpu_free_probe=gpu_probe,
                )
                logger.info(
                    # rep_model_key 是 llm group 的「代表标识」(取 llm_specs[0].id),
                    # **不是启动加载目标** —— 它 status 一直 unloaded。实际加载由
                    # resident preload / 工作流执行时的按需 get_or_load / 手动决定。
                    # 旧文案打 `model_key=%s` 易被误读成「启动加载了这个模型」(排查
                    # startup 自动加载时踩过坑,见 memory project_startup_model_load_paths)。
                    "Lane K: LLMRunner instantiated (group=%s, gpus=%s, "
                    "rep_model_key=%s [group 代表标识,非启动加载目标], adapter_present=%s)",
                    group.id, group.gpus, rep_model_key, rep_adapter is not None,
                )
            else:
                # image / tts group → fork runner 子进程 + 建 client。
                sup = RunnerSupervisor(
                    group_id=group.id,
                    gpus=list(group.gpus),
                    models_yaml_path=models_yaml_path,
                    fake_adapter=False,
                    gpu_free_probe=gpu_probe,
                )
                try:
                    await sup.start()
                except Exception:
                    logger.exception(
                        "Lane K: failed to start RunnerSupervisor for group %s — "
                        "continuing without it (fail-soft, /health will report degraded)",
                        group.id,
                    )
                    continue
                runner_supervisors.append(sup)
                # sup.client 是 supervisor._spawn 建好的 RunnerClient —— 复用即可,
                # 不再自己新建一个（每对 pipe 只能有一个 reader）。
                if sup.client is not None:
                    runner_clients[group.id] = sup.client
                logger.info(
                    "Lane K: RunnerSupervisor spawned (group=%s, gpus=%s, pid=%s)",
                    group.id, group.gpus, sup.pid,
                )

    app.state.runner_supervisors = runner_supervisors
    app.state.runner_clients = runner_clients
    app.state.llm_runner = llm_runner

    # PR-5a: component-state mirror fed by the image runner's ComponentEvents.
    from src.services.component_state import ComponentStateRegistry
    app.state.component_state_registry = ComponentStateRegistry()
    _img_client = runner_clients.get("image")
    if _img_client is not None:
        _img_client.on_component_event = _make_component_event_handler(
            app.state.component_state_registry, ws_manager)

    # Auto-detect running vLLM instances BEFORE resident auto-load
    # (so we reconnect to orphans instead of spawning duplicates)
    from src.services.inference.vllm_scanner import scan_running_vllm
    running_vllm = scan_running_vllm()
    if running_vllm:
        logger.info("Found %d running vLLM process(es)", len(running_vllm))
    reconnected: set[str] = set()
    for vllm_info in running_vllm:
        matched_spec = next(
            (sp for sp in registry.specs
             if sp.model_type == "llm" and sp.paths.get("main")
             and vllm_info["model_path"].rstrip("/").endswith(sp.paths["main"].rstrip("/"))),
            None,
        )
        action = _classify_orphan(bool(vllm_info["healthy"]), matched_spec is not None)
        if action == "kill":
            logger.warning(
                "Killing unhealthy orphan vLLM (pid=%d, port=%d, model=%s)",
                vllm_info["pid"], vllm_info["port"], vllm_info["model_path"],
            )
            _kill_orphan_vllm(vllm_info["pid"])
            continue
        if action == "kill_unmatched":
            logger.error(
                "目录里已没有对应 spec 的 vLLM 还在跑（pid=%d, port=%d, model=%s）"
                " —— 没人会接管它，它却抱着显存把常驻模型顶死，现在收掉",
                vllm_info["pid"], vllm_info["port"], vllm_info["model_path"],
            )
            _kill_orphan_vllm(vllm_info["pid"])
            continue

        # Reconnect healthy ones（action == "adopt"，matched_spec 必非 None）
        spec = matched_spec
        logger.info(
            "Reconnecting to running vLLM for %s (pid=%s, port=%s)",
            spec.id, vllm_info["pid"], vllm_info["port"],
        )
        try:
            def _factory(s, port=vllm_info["port"], pid=vllm_info["pid"]):
                from src.services.inference.llm_vllm import VLLMAdapter
                # gpus:重连的也可能是个跨卡(张量并行)实例 —— 带上组,
                # 否则 adapter 眼里它是单卡的,后续任何重启都会落错卡。
                from src.gpu.topology import resolve_gpus as _rg
                _g = _rg(s)
                return VLLMAdapter(paths=s.paths, vllm_port=port, adopt_pid=pid,
                                   gpus=_g if len(_g) > 1 else None, **s.params)
            await model_mgr.load_model(spec.id, adapter_factory=_factory)
            reconnected.add(spec.id)
        except Exception as e:
            logger.warning("Failed to reconnect %s: %s", spec.id, e)

    # 开机加载策略(2026-09-03 收敛,与 UI 语义对齐):
    #   * 只有 `resident: true` 的模型会被开机预加载(下面 preload_residents)。
    #   * 已发布工作流引用到的模型 **不** 预加载 —— 工作流真正执行时 runner
    #     走 get_or_load 按需加载(首调慢一点,不占开机显存)。
    #   * 下面这轮对账登记的模型引用只用于「防卸载」(挡 idle checker / LRU 驱逐),
    #     登记本身绝不触发 load。
    #   * 其余加载都由 UI 手动触发。

    # Re-register model references for published workflows
    from src.services.startup_reconcile import reconcile_orphan_published_workflows
    async with sf() as session:
        orphan_published = await reconcile_orphan_published_workflows(
            session, model_mgr,
        )
        if orphan_published:
            logger.info(
                "startup: reconciled %d orphan published workflows → draft (no linked service)",
                orphan_published,
            )

    # 上次进程遗留的在飞任务(queued/running)→ failed。执行器全是本进程内的 asyncio
    # task,进程一死它们就没了,DB 行却永远停在 running(前端永远 processing 的孤儿)。
    # 详见 startup_reconcile.reconcile_orphan_inflight_tasks 的 docstring。
    from src.services.startup_reconcile import reconcile_orphan_inflight_tasks
    async with sf() as session:
        orphan_inflight = await reconcile_orphan_inflight_tasks(session)
        if orphan_inflight:
            logger.info(
                "startup: %d orphan in-flight execution task(s) → failed",
                orphan_inflight,
            )

    # One-time + self-healing reconcile: re-derive category/meter_dim for
    # workflow-sourced services from their frozen snapshot. Historically the
    # image detector only recognized the flux2_vae_decode terminus, so services
    # published through the integrated image_generate node froze as category
    # "app" + meter_dim "calls" — misfiled in the UI AND mis-metered (billed as
    # generic calls, not images). The helper only upgrades to "image", never
    # clobbering an explicitly-locked llm/tts/vl category.
    from src.api.routes.workflow_publish import reconcile_service_categories
    async with sf() as session:
        recategorized = await reconcile_service_categories(session)
        if recategorized:
            await session.commit()
            from src.api.response_cache import invalidate
            invalidate("services")
            logger.info(
                "startup: reconciled %d service categories from snapshot (image misfiled as app)",
                recategorized,
            )

    bg_tasks, cache_cleanup_task, response_cleanup_task, partial_worker = (
        _start_background_tasks(app, model_mgr)
    )

    # 启动自检 banner(对齐 PAPERCLIP)—— 全 wiring 完后打一屏聚合状态到 stdout/journald。
    # 测试态(catch-all 关)跳过避免噪声;全 best-effort,绝不阻断启动。
    if os.environ.get("NOUS_DISABLE_FRONTEND_MOUNT") != "1":
        try:
            from src.api.startup_banner import log_startup_banner
            await log_startup_banner(app)
        except Exception:  # noqa: BLE001 — banner 失败不该影响启动
            logger.warning("startup banner 调用失败(忽略)", exc_info=True)

    try:
        yield
    finally:
        # Gracefully shut down background tasks (only if they were started)
        if cache_cleanup_task is not None:
            cache_cleanup_task.cancel()
        if response_cleanup_task is not None:
            response_cleanup_task.cancel()
        for t in (cache_cleanup_task, response_cleanup_task):
            if t is None:
                continue
            try:
                await t
            except asyncio.CancelledError:
                pass
        # round4 #9:cancel + await 常驻后台 loop(idle/memory_guard/log_cleanup/
        # orphan_reap),否则它们随 loop 关闭被硬杀,可能在
        # check_idle_models / nvidia-smi poll 中途留半完成状态。
        for t in bg_tasks:
            t.cancel()
        # gather(return_exceptions=True) 排空已取消的后台任务:task 的 CancelledError
        # 作为结果收集而非泛化吞掉;若 lifespan 本身被取消,gather 会正确向上传播
        # (A4:不再 bare except 吞 CancelledError,符合结构化并发语义)。
        await asyncio.gather(*bg_tasks, return_exceptions=True)
        # Flush + stop the structured-log writer (drains queued log rows).
        try:
            from src.services.log_store import log_writer
            await log_writer.stop()
        except Exception:  # noqa: BLE001
            pass
        # Drain partial-write worker
        if partial_worker is not None:
            from src.api.routes import responses as responses_routes
            if responses_routes._partial_write_queue is not None:
                await responses_routes._partial_write_queue.put(None)
                try:
                    await asyncio.wait_for(partial_worker, timeout=5.0)
                except asyncio.TimeoutError:
                    partial_worker.cancel()

        # Lane K: stop runner supervisors + LLMRunner (terminate child subprocs).
        # getattr 兜底:NOUS_DISABLE_RUNNER_SPAWN 默认 skip 时这些属性不一定有.
        for sup in getattr(app.state, "runner_supervisors", []) or []:
            try:
                await sup.stop()
            except Exception:
                logger.exception("Lane K: RunnerSupervisor stop failed (group=%s)",
                                 getattr(sup, "group_id", "?"))
        _llm = getattr(app.state, "llm_runner", None)
        if _llm is not None:
            try:
                await _llm.shutdown()
            except Exception:
                logger.exception("Lane K: LLMRunner shutdown failed")

        # vLLM 子进程是 model_manager 直接 spawn 的(VLLMAdapter._process / EngineCore),
        # **不归** runner supervisors / llm_runner 管 —— 上面 stop 完它们,vLLM 仍活着。
        # 不在此 force-unload,backend 退出后 vLLM 成 orphan 继续占显存:反复重启时每个
        # ~40G 累积,把常驻 LLM 的卡占爆 → image 出图 OOM(真机实锤:Pro6000 被 2 个
        # orphan vLLM + 当前 vLLM 占满,只剩 12G < 需 22.8G)。force-unload 走
        # adapter.unload() → killpg 整个进程组,连 EngineCore worker 一起收。
        # in-use 守卫仍强于 force(正在 infer 的不卸,避免 segfault)。
        _mm = getattr(app.state, "model_manager", None)
        if _mm is not None:
            for mid in list(getattr(_mm, "loaded_model_ids", []) or []):
                try:
                    await _mm.unload_model(mid, force=True)
                except Exception:
                    logger.exception(
                        "shutdown: force-unload %s failed (vLLM 可能 orphan)", mid)


def create_app() -> FastAPI:
    app = FastAPI(title="Nous Center", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://localhost:3000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # ETag is exposed so JS clients can read it for explicit If-None-Match
        # validation. The browser's native HTTP cache uses ETag transparently
        # regardless, but custom diagnostic / instrumentation code needs CORS
        # to expose the header.
        expose_headers=["X-Request-Id", "ETag"],
    )

    from src.api.middleware import (
        RequestLoggingMiddleware,
        AuditMiddleware,
        RequestIdMiddleware,
        AdminSessionGateMiddleware,
        SecurityHeadersMiddleware,
    )
    # 安全响应头(二轮安全 §7):所有响应注入 X-Frame-Options/nosniff/Referrer-Policy。
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    # Admin session gate sits before logging so 401s still get a request id but
    # don't reach business handlers.
    app.add_middleware(AdminSessionGateMiddleware)
    # LIFO: RequestIdMiddleware added LAST so it runs FIRST, populating
    # request.state.request_id before exception handlers inspect it.
    app.add_middleware(RequestIdMiddleware)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/health")
    async def health_check():
        checks: dict = {"status": "ok"}

        # Check database
        try:
            from src.models.database import get_session_factory
            from sqlalchemy import text
            _sf = get_session_factory()
            async with _sf() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            checks["database"] = "error"
            checks["status"] = "degraded"

        # GPU availability
        from src.services.gpu_monitor import get_gpu_stats
        gpus = get_gpu_stats()
        checks["gpus"] = len(gpus)

        # Loaded models + resident-preload failures (spec 4.3). A non-empty
        # load_failures dict means at least one resident model failed to
        # preload — the Dashboard renders a degraded banner + Retry from this.
        mgr = getattr(app.state, "model_manager", None)
        checks["models_loaded"] = len(mgr.loaded_model_ids) if mgr else 0
        load_failures = dict(mgr._load_failures) if mgr else {}
        checks["load_failures"] = load_failures
        if load_failures:
            checks["status"] = "degraded"

        # 启动/加载提示:resident 模型预加载进度 —— 重启后 vLLM/image 在后台重载的窗口里,
        # 前端据此挂「系统启动中·模型加载 M/N」全局横幅(用户要的「启动提示」)。
        # 用 registry 所有 resident spec(含 llm/embedding/image)vs is_loaded,口径统一。
        # 防御:mgr 可能是没有 _registry 的替身(测试 mock)/ 取不到时降级成空,绝不让 /health 500。
        r_total = r_loaded = 0
        try:
            reg = getattr(mgr, "_registry", None) if mgr else None
            specs = getattr(reg, "specs", None) or []
            resident_specs = [s for s in specs if getattr(s, "resident", False)]
            r_total = len(resident_specs)
            r_loaded = sum(1 for s in resident_specs if mgr.is_loaded(s.id))
        except Exception:  # noqa: BLE001 — 启动提示是 best-effort,坏了不拖垮 /health
            r_total = r_loaded = 0
        # preloading 必须绑「preload 任务真的在跑」:旧逻辑 r_total > r_loaded 是纯状态
        # 比较,resident 模型被 TTL 卸载/加载失败后,运行了几小时的系统也会永远挂
        # 「系统启动中·刚重启」横幅(2026-07-05 用户报障根因)。失败态走 load_failures
        # → degraded,不归启动横幅管。
        _pt = getattr(app.state, "_resident_preload_task", None)
        preload_running = _pt is not None and not _pt.done()
        checks["startup"] = {
            "resident_total": r_total,
            "resident_loaded": r_loaded,
            "preloading": preload_running and r_total > r_loaded,
        }

        # Per-runner state (spec 4.2). runner_supervisors is populated by Lane K
        # lifespan wiring; until then it's unset and runners is []. LLMRunner
        # (主进程对象, app.state.llm_runner) 也并入此列表 —— 它有自己的
        # health_snapshot()，让前端 TaskPanel 用同一个 runners 列表渲染所有泳道。
        #
        # degraded 判定按各 runner 自报的 `healthy`,**不是** `running`:LLMRunner 在本
        # 架构稳定停在 IDLE(从不自己 spawn vLLM —— vLLM 由 model_mgr 懒加载/常驻预载
        # 路径起),IDLE 是健康待命态,`running` 恒 False。旧逻辑 `not running` 把它误判成
        # degraded → /health 永久 degraded(公开状态页跟着误报黄灯)。各 runner 自己定义
        # healthy(supervisor: 子进程在跑;LLMRunner: 非 FAILED)。缺 healthy 字段的旧/替身
        # 快照回退到 running,保持兼容。
        supervisors = getattr(app.state, "runner_supervisors", [])
        runners = [s.health_snapshot() for s in supervisors]
        _llm = getattr(app.state, "llm_runner", None)
        if _llm is not None:
            runners.append(_llm.health_snapshot())
        checks["runners"] = runners
        if any(not r.get("healthy", r.get("running", False)) for r in runners):
            checks["status"] = "degraded"

        return checks
    app.include_router(understand.router)
    app.include_router(generate.router)
    app.include_router(tts.router)
    app.include_router(engines.router)
    app.include_router(engines.gpu_router)
    app.include_router(models_routes.router)
    app.include_router(loras_routes.router)
    app.include_router(image_files_routes.router)
    from src.api.routes import components as components_routes
    app.include_router(components_routes.router)
    app.include_router(audio.router)
    app.include_router(voices.router)
    app.include_router(openai_compat.router)
    app.include_router(ollama_compat.router)
    app.include_router(api_gateway_routes.router)
    app.include_router(context_cache_routes.router)
    app.include_router(files_routes.router)
    from src.api.routes import responses as responses_routes
    app.include_router(responses_routes.router)
    app.include_router(settings.router)
    # legacy /api/v1/instances 已删(双轨收敛 #3):读走 v3 /services,建模型服务走
    # /services/register-model。
    app.include_router(predictions_routes.router)
    app.include_router(workflows.router)
    app.include_router(agents.router)
    app.include_router(skills.router)
    app.include_router(monitor.router)
    app.include_router(node_packages.router)
    app.include_router(execution_tasks.router)
    app.include_router(apps.router)
    app.include_router(services_routes.router)
    app.include_router(comfy_templates_routes.router)
    app.include_router(comfy_templates_routes.health_router)
    app.include_router(workflow_publish_routes.router)
    from src.api.routes import external_providers as external_providers_routes
    app.include_router(external_providers_routes.router)
    app.include_router(usage_routes.router)
    app.include_router(dashboard_routes.router)
    app.include_router(api_keys_routes.router)
    app.include_router(api_keys_routes.service_grants_router)
    app.include_router(anthropic_compat.router)
    app.include_router(observability.router)
    from src.api.routes import status as status_routes
    app.include_router(status_routes.router)
    app.include_router(logs.router)
    from src.api.routes import memory as memory_routes
    app.include_router(memory_routes.router)
    from src.api.routes import admin_auth as admin_auth_routes
    app.include_router(admin_auth_routes.router)
    from src.api.routes import admin_passkey as admin_passkey_routes
    app.include_router(admin_passkey_routes.router)
    from src.api.routes import admin_totp as admin_totp_routes
    app.include_router(admin_totp_routes.router)

    from src.api.admin_session import websocket_is_authed

    async def _reject_unauthed_ws(websocket: WebSocket) -> bool:
        """Return True when the WS was rejected. Closes with policy code 4401."""
        if websocket_is_authed(websocket):
            return False
        await websocket.close(code=4401)
        return True

    @app.websocket("/ws/tasks/{task_id}")
    async def websocket_task(websocket: WebSocket, task_id: str):
        if await _reject_unauthed_ws(websocket):
            return
        await ws_manager.connect(task_id, websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_manager.disconnect(task_id, websocket)

    @app.websocket("/ws/tasks")
    async def websocket_tasks_global(websocket: WebSocket):
        """Global task list WebSocket — pushes task create/update/delete events."""
        if await _reject_unauthed_ws(websocket):
            return
        await ws_manager.subscribe_global(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_manager.unsubscribe_global(websocket)

    @app.websocket("/ws/models")
    async def websocket_models(websocket: WebSocket):
        """Model loading status WebSocket -- pushes loading/loaded/failed events."""
        if await _reject_unauthed_ws(websocket):
            return
        await ws_manager.subscribe_models(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_manager.unsubscribe_models(websocket)

    @app.websocket("/ws/tts")
    async def websocket_tts(websocket: WebSocket):
        if await _reject_unauthed_ws(websocket):
            return
        await handle_tts_websocket(websocket)

    @app.websocket("/ws/workflow/{instance_id}")
    async def workflow_progress_ws(websocket: WebSocket, instance_id: str):
        if await _reject_unauthed_ws(websocket):
            return
        await websocket.accept()
        if instance_id not in _ws_connections:
            _ws_connections[instance_id] = []
        _ws_connections[instance_id].append(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            # round4 #12:workflow_runner._broadcast 推送失败时会先 remove 本连接、桶空再
            # pop 掉整个 key。竞态下若本连接已被它剔除、key 已删,这里裸 `[instance_id].remove`
            # 会抛 KeyError/ValueError(从 except 逃逸成未处理 task 异常)。且非干净断开
            # (网络 reset 不抛 WebSocketDisconnect)早先完全不清理 → 连接 + key 永久泄漏。
            # 改 finally + 守卫:存在才 remove,空了再 pop。
            socks = _ws_connections.get(instance_id)
            if socks and websocket in socks:
                socks.remove(websocket)
                if not socks:
                    _ws_connections.pop(instance_id, None)

    _mount_frontend(app)
    _register_error_handlers(app)
    return app


# --------------------------------------------------------------------------- #
# Frontend static serving — production builds are served by the API process so
# the browser hits one origin (cloudflared can point at :8000 only). Vite dev
# on :9999 still works for local HMR; this only kicks in when `dist/` exists.
# --------------------------------------------------------------------------- #

# Path prefixes that belong to the API and must NOT fall through to the SPA.
# Anything else returns index.html so client-side routing handles deep links.
_API_PREFIXES = ("/v1", "/sys", "/api", "/ws", "/health", "/healthz", "/docs", "/openapi.json", "/redoc")


def _frontend_dist_dir() -> Path | None:
    # backend/src/api/main.py → repo_root = parents[3]
    candidate = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    return candidate if (candidate / "index.html").exists() else None


def _mount_frontend(app: FastAPI) -> None:
    # Test suites build the app and add their own routes after create_app().
    # The SPA catch-all (/{full_path:path}) would otherwise win route matching
    # against later-registered test endpoints. Tests set this env to opt out.
    import os
    if os.environ.get("NOUS_DISABLE_FRONTEND_MOUNT") == "1":
        return
    dist = _frontend_dist_dir()
    if dist is None:
        logger.info("frontend dist not found, skipping static mount (run `npm run build`)")
        return

    index_html = dist / "index.html"
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="frontend-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str, request: Request):
        path = "/" + full_path
        if any(path == p or path.startswith(p + "/") for p in _API_PREFIXES):
            raise HTTPException(status_code=404)
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index_html)


# --------------------------------------------------------------------------- #
# Global exception handlers — convert everything into OpenAI-style JSON
# --------------------------------------------------------------------------- #

from src.errors import (
    NousError,
    InvalidRequestError,
    AuthenticationError,
    PermissionError as NousPermissionError,
    NotFoundError,
    RateLimitError,
    APIError,
)

_HTTP_STATUS_TO_ERROR = {
    400: InvalidRequestError,
    401: AuthenticationError,
    403: NousPermissionError,
    404: NotFoundError,
    409: InvalidRequestError,
    422: InvalidRequestError,
    429: RateLimitError,
}

# Default `code` field for statuses where the shared error type needs disambiguation
_STATUS_DEFAULT_CODE = {
    409: "conflict",
    422: "validation_error",
}


def _detail_to_message_and_param(detail) -> tuple[str, str | None]:
    """Parse HTTPException.detail into (message, param).

    detail can be str, list (Pydantic-style), or anything else.
    """
    if isinstance(detail, str):
        return detail, None
    if isinstance(detail, list) and detail:
        first = detail[0] if isinstance(detail[0], dict) else {}
        msg = first.get("msg") or "; ".join(
            e.get("msg", str(e)) if isinstance(e, dict) else str(e) for e in detail
        )
        loc = first.get("loc") or []
        param = ".".join(str(x) for x in loc if x != "body") or None
        return msg, param
    return str(detail), None


def _response(err: NousError) -> JSONResponse:
    headers = {"X-Request-Id": err.request_id} if err.request_id else {}
    return JSONResponse(err.to_dict(), status_code=err.http_status, headers=headers)


def _with_request_id(err: NousError, request) -> NousError:
    err.request_id = getattr(request.state, "request_id", None)
    return err


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NousError)
    async def _nous(request, exc: NousError):
        return _response(_with_request_id(exc, request))

    @app.exception_handler(HTTPException)
    async def _http(request, exc: HTTPException):
        status = exc.status_code
        cls = _HTTP_STATUS_TO_ERROR.get(status)
        if cls is None:
            cls = InvalidRequestError if 400 <= status < 500 else APIError
        msg, param = _detail_to_message_and_param(exc.detail)
        err = cls(msg, param=param, code=_STATUS_DEFAULT_CODE.get(status))
        err.http_status = status  # preserve original 4xx nuance
        return _response(_with_request_id(err, request))

    @app.exception_handler(RequestValidationError)
    async def _validation(request, exc: RequestValidationError):
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        err = InvalidRequestError(
            message=first.get("msg", "Invalid request"),
            code="validation_error",
            param=loc or None,
        )
        return _response(_with_request_id(err, request))

    @app.exception_handler(Exception)
    async def _unhandled(request, exc: Exception):
        # Outer safety net: this handler itself must never raise.
        rid = None
        try:
            rid = getattr(request.state, "request_id", None)
            try:
                logger.exception(
                    "unhandled exception | req_id=%s | %s %s",
                    rid, request.method, request.url.path,
                )
            except Exception:
                pass  # logging failure must not crash the handler
            err = APIError(
                "Internal server error", code="internal_error", request_id=rid
            )
            return _response(err)
        except Exception:
            headers = {"X-Request-Id": rid} if rid else {}
            return JSONResponse(
                {"error": {
                    "message": "Internal server error",
                    "type": "api_error",
                    "code": "internal_error",
                }},
                status_code=500,
                headers=headers,
            )


app = create_app()
