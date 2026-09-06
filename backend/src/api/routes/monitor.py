"""Real-time system resource monitoring endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time

import psutil
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.api.deps_admin import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/monitor", tags=["monitor"])


def _smi_int(s: str, default: int = 0) -> int:
    """nvidia-smi 字段 → int,`[N/A]`/空/非数字降级 default(round5,防全卡消失)。"""
    s = (s or "").strip()
    if not s or s == "[N/A]":
        return default
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


def _smi_float(s: str, default: float = 0.0) -> float:
    s = (s or "").strip()
    if not s or s == "[N/A]":
        return default
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _gpu_stats_nvidia_smi() -> list[dict] | None:
    """Query nvidia-smi for GPU stats. Returns None on failure."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu,fan.speed,power.draw,power.limit",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None

        gpus = []
        for line in result.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 9:
                continue
            # round5:util/temp/mem 早先无条件 int() —— 某卡 util/temp 报 `[N/A]`
            # (被动散热卡/特定驱动状态)时 int("[N/A]") ValueError,而整循环包在外层单个
            # try 里 → 不是丢一张卡,而是 gpus 整体变 []、所有 GPU 从面板消失。用 _smi_int
            # 守卫(对齐 fan/power 已有的 != "[N/A]"),并 per-line try:坏行 continue。
            try:
                mem_used = _smi_int(parts[2])
                mem_total = _smi_int(parts[3])
                gpus.append(
                    {
                        "index": _smi_int(parts[0]),
                        "name": parts[1],
                        "utilization_gpu": _smi_int(parts[4]),
                        "utilization_memory": round(mem_used / mem_total * 100, 1)
                        if mem_total > 0
                        else 0,
                        "temperature": _smi_int(parts[5]),
                        "fan_speed": _smi_int(parts[6]),
                        "power_draw_w": _smi_float(parts[7]),
                        "power_limit_w": _smi_float(parts[8]),
                        "memory_used_mb": mem_used,
                        "memory_total_mb": mem_total,
                        "memory_free_mb": mem_total - mem_used,
                        "processes": [],
                    }
                )
            except Exception:  # noqa: BLE001 — 单卡坏行不该清空整张表
                logger.warning("nvidia-smi 行解析失败,跳过: %r", line)
                continue
        return gpus
    except Exception:
        return None


def _gpu_processes(pid_map: dict[int, str] | None = None) -> dict[int, list[dict]]:
    """Get per-GPU process memory usage via nvidia-smi, enriched with process info."""
    if pid_map is None:
        pid_map = {}
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {}

        # Map GPU UUID -> index
        uuid_result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        uuid_to_idx: dict[str, int] = {}
        if uuid_result.returncode == 0:
            for line in uuid_result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    uuid_to_idx[parts[1]] = int(parts[0])

        procs: dict[int, list[dict]] = {}
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            gpu_uuid = parts[0]
            gpu_idx = uuid_to_idx.get(gpu_uuid, -1)
            if gpu_idx < 0:
                continue

            pid = int(parts[1])
            mem_mb = int(parts[2])

            # Enrich with process name/command via psutil
            name = ""
            command = ""
            command_full = ""
            managed = pid in pid_map
            model_name = pid_map.get(pid)
            try:
                p = psutil.Process(pid)
                name = p.name()
                cmdline = p.cmdline()
                if cmdline:
                    command_full = " ".join(cmdline)
                    # Short version for inline list display(避免溢出);完整版给前端
                    # 做 hover tooltip,用户能一眼看到这是 nous runner / ComfyUI / 谁。
                    command = " ".join(cmdline[:8])[:120]
                else:
                    command_full = name
                    command = name

                # multiprocessing.spawn 产生的 child(RunnerSupervisor → child → grandchild)
                # 让 GPU 进程的 PID **不等于** Supervisor 记录的 PID。向上爬 parent chain,
                # 任一 ancestor 在 pid_map 就归类为 managed,避免活跃 runner 被误标 orphan。
                if not managed:
                    try:
                        for ancestor in p.parents():
                            if ancestor.pid in pid_map:
                                managed = True
                                model_name = pid_map[ancestor.pid]
                                break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            procs.setdefault(gpu_idx, []).append(
                {
                    "pid": pid,
                    "gpu": gpu_idx,
                    "used_gpu_memory_mb": mem_mb,
                    "name": name,
                    "command": command,
                    "command_full": command_full,
                    "managed": managed,
                    "model_name": model_name,
                }
            )
        return procs
    except Exception:
        return {}


# spec 2026-09-06 process-net-traffic §2.2:每进程网络速率来自 root 采集器
# (nous-engine-netprobe)每 2s 原子写的文件;本进程(heygo)只读,不提权。
NET_BY_PID_DEFAULT = "/run/nous-engine/net_by_pid.json"
NET_BY_PID_MAX_AGE_S = 10.0
_net_probe_last_available: bool | None = None


def _read_net_by_pid(
    path: str | None = None,
    max_age_s: float = NET_BY_PID_MAX_AGE_S,
    now: float | None = None,
) -> tuple[dict[int, tuple[int, int]], dict]:
    """读采集器文件 → ({pid: (tx_bps, rx_bps)}, net_probe)。缺失/过期/坏 JSON 一律
    ({}, available=False),不抛;每 2s 轮询都会调,只在可用性**翻转**时记一行 info。"""
    global _net_probe_last_available
    path = path or os.environ.get("NOUS_NET_BY_PID", NET_BY_PID_DEFAULT)
    now = time.time() if now is None else now
    net: dict[int, tuple[int, int]] = {}
    available = False
    age: float | None = None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        age = round(now - float(doc["ts"]), 1)
        interval = float(doc.get("interval_s") or 2) or 2.0
        if 0 <= age <= max_age_s:
            available = True
            for pid_s, v in (doc.get("pids") or {}).items():
                try:
                    net[int(pid_s)] = (
                        int(int(v.get("tx", 0)) / interval),
                        int(int(v.get("rx", 0)) / interval),
                    )
                except (TypeError, ValueError, AttributeError):
                    continue
    except (OSError, ValueError, KeyError, TypeError):
        net, available, age = {}, False, None
    if available != _net_probe_last_available:
        logger.info("netprobe %s(%s)", "可用" if available else "不可用", path)
        _net_probe_last_available = available
    return net, {"available": available, "age_s": age}


def _top_processes(
    limit: int = 20,
    net: dict[int, tuple[int, int]] | None = None,
    max_total: int = 40,
) -> list[dict]:
    """CPU 前 limit 个进程 ∪ 有网络流量但不在其中的进程(按流量降序补,总数 ≤ max_total)。

    net 为 None = 采集器不可用 → net_*_bps 全 None;为 {} = 可用但没流量 → 全 0。
    """
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "cmdline"]):
        try:
            info = p.info
            procs.append(
                {
                    "pid": info["pid"],
                    "name": info["name"] or "",
                    "cpu_percent": info["cpu_percent"] or 0.0,
                    "memory_mb": round((info["memory_info"].rss if info["memory_info"] else 0) / 1024**2),
                    "command": " ".join(info["cmdline"][:5]) if info["cmdline"] else info["name"] or "",
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    procs.sort(key=lambda x: x["cpu_percent"], reverse=True)
    top = procs[:limit]
    if net:
        chosen = {r["pid"] for r in top}
        by_pid = {r["pid"]: r for r in procs}
        extra = sorted(
            (pid for pid in net if pid not in chosen and pid in by_pid and sum(net[pid]) > 0),
            key=lambda pid: sum(net[pid]),
            reverse=True,
        )
        top = top + [by_pid[pid] for pid in extra[: max(0, max_total - len(top))]]
    for r in top:
        if net is None:
            r["net_tx_bps"] = None
            r["net_rx_bps"] = None
        else:
            tx, rx = net.get(r["pid"], (0, 0))
            r["net_tx_bps"] = tx
            r["net_rx_bps"] = rx
    return top


# 短 TTL 服务端缓存(性能 P0):/monitor/stats 无缓存,被前端两个独立 query key
# 各 2s 轮询,每次 nvidia-smi 子进程 + 全主机 process_iter + cpu_percent(0.1)。
# 1.5s TTL 把重复计算折叠掉(双 key 轮询命中缓存),asyncio.Lock 防缓存 miss 时
# 并发惊群(N 个请求只算一次)。live 数据,过期即重算,无需失效逻辑。
_STATS_TTL_S = 1.5
_stats_cache: dict = {"ts": -1e9, "payload": None}
_stats_lock: "asyncio.Lock | None" = None


def _reset_stats_cache() -> None:
    """测试钩子:清缓存,避免跨测试(TTL 内)串用上一个测试的 mock 结果。"""
    _stats_cache["ts"] = -1e9
    _stats_cache["payload"] = None


@router.get("/stats")
async def get_system_stats(request: Request):
    """Return real-time system resource usage (short-TTL cached)."""
    global _stats_lock
    now = time.monotonic()
    cached = _stats_cache["payload"]
    if cached is not None and now - _stats_cache["ts"] < _STATS_TTL_S:
        return cached
    if _stats_lock is None:
        _stats_lock = asyncio.Lock()
    async with _stats_lock:
        # 二次确认:等锁期间别的协程可能已填好。
        now = time.monotonic()
        if _stats_cache["payload"] is not None and now - _stats_cache["ts"] < _STATS_TTL_S:
            return _stats_cache["payload"]
        payload = await _compute_system_stats(request)
        _stats_cache["payload"] = payload
        _stats_cache["ts"] = time.monotonic()
        return payload


async def _compute_system_stats(request: Request):
    """实际采集系统资源(nvidia-smi / psutil)。经 get_system_stats 的 TTL 缓存包裹。"""
    import asyncio

    # GPU stats via nvidia-smi — 同步 subprocess(nvidia-smi, timeout=5),前端每 2s 轮询;
    # 直接在事件循环上跑会周期性阻塞所有并发请求(含流式推理)。丢线程池。性能 P1-1。
    gpus = await asyncio.to_thread(_gpu_stats_nvidia_smi) or []

    # Get PID map for managed/orphan detection.
    #
    # 三类受管子进程都要纳入,否则 ProcRow 会把活跃 runner 误标 orphan(red bg + kill 按钮),
    # 误导操作员 kill 掉自己的 image runner:
    # 1. **常驻 LLM**(`model_manager._models[*].adapter.pid`)— vLLM/sgLang inference 进程
    # 2. **RunnerSupervisor 子进程**(`app.state.runner_supervisors[*].pid`)— image / tts /
    #    llm-tp 等按 hardware.yaml group spawn 的常驻 runner(本地用户报告的 2501948 = image
    #    runner subprocess,Pro 6000 上常驻 ~38GB)
    # 3. **主进程 LLM runner**(`app.state.llm_runner` 当无 hardware.yaml 时)— 进程内对象,
    #    跟主 backend 共享 PID,这里跳过(主 backend PID 不该出现在 GPU compute-apps 里;
    #    若出现,也属于 backend 本体)
    pid_map: dict[int, str] = {}
    model_mgr = getattr(request.app.state, "model_manager", None)
    if model_mgr is not None:
        pid_map.update(model_mgr.get_pid_map())
    for sup in getattr(request.app.state, "runner_supervisors", []) or []:
        sup_pid = getattr(sup, "pid", None)
        if sup_pid is not None:
            # group_id 形如 "image" / "tts" / "llm-tp" — 当 model_name 展示给 UI。
            pid_map[sup_pid] = f"runner:{getattr(sup, 'group_id', 'unknown')}"

    # Attach per-GPU process info (又是两次 nvidia-smi subprocess + psutil 遍历 → 线程池)
    if gpus:
        gpu_procs = await asyncio.to_thread(_gpu_processes, pid_map)
        for gpu in gpus:
            gpu["processes"] = gpu_procs.get(gpu["index"], [])

    # Add memory warning flags
    from src.services.gpu_monitor import DEFAULT_RESERVED_GB
    for gpu in gpus:
        free_gb = gpu["memory_free_mb"] / 1024
        gpu["low_memory"] = free_gb < DEFAULT_RESERVED_GB

    # Add loaded models to GPU info
    from src.config import load_model_configs
    from src.gpu.detector import get_device_for_engine
    from src.gpu.topology import resolve_gpus

    configs = load_model_configs()
    loaded = model_mgr.loaded_model_ids if model_mgr is not None else []
    for model_key in loaded:
        cfg = configs.get(model_key, {})
        # 张量并行的模型占多张卡 —— 只读 cfg["gpu"] 会让它只出现在一张卡下(审查 #23)。
        # 真实落卡(manager 的 gpu_indices)优先于配置;都没有才让 detector 推断。
        entry = getattr(model_mgr, "_models", {}).get(model_key) if model_mgr else None
        cards = list(entry.cards()) if entry is not None else resolve_gpus(cfg)
        if not cards:
            device = get_device_for_engine(cfg)
            if device.startswith("cuda:"):
                try:
                    cards = [int(device.split(":")[-1])]
                except ValueError:
                    cards = []
        for gpu_idx in cards:
            # Find the GPU in our list and add the model
            matched = False
            for g in gpus:
                if g["index"] == gpu_idx:
                    matched = True
                    if "loaded_models" not in g:
                        g["loaded_models"] = []
                    g["loaded_models"].append({
                        "name": model_key,
                        "type": cfg.get("type", ""),
                        "vram_gb": cfg.get("vram_gb", 0),
                    })
            if not matched:
                # gpu_idx 越界(如 yaml 写 cuda:3 但只有 0/1/2)—— 旧逻辑静默丢,模型从
                # 系统状态「已加载」凭空消失。改成 warning(不再静默),便于发现配置错。
                logger.warning(
                    "monitor: 已加载模型 %r 的 gpu=%s 不在检测到的 GPU 列表 → 系统状态漏显示(查 yaml/配置)",
                    model_key, gpu_idx,
                )

    # runner 子进程里加载的 image/tts adapter —— 主进程 _models 看不到它们,从各
    # supervisor 经 Pong 上报的快照聚合补进 GPU 的 loaded_models(否则系统状态恒「0」)。
    # 名字取源组件文件 basename(adapter 是 combo hash id,对用户没意义)。
    from src.services.runner_models import aggregate_runner_loaded
    import os as _os
    for entry in aggregate_runner_loaded(request.app.state):
        if entry.get("group_id") == "main":
            continue  # 主进程模型已在上面 loaded_model_ids 那轮覆盖
        gpu_idx = entry.get("gpu_index")
        if not isinstance(gpu_idx, int):
            continue
        srcs = entry.get("source_files") or []
        name = _os.path.basename(srcs[0]) if srcs else entry.get("model_id", "?")
        for g in gpus:
            if g["index"] == gpu_idx:
                g.setdefault("loaded_models", []).append({
                    "name": name,
                    "type": entry.get("model_type", ""),
                    "vram_gb": round((entry.get("vram_mb") or 0) / 1024, 1),
                })

    # CPU stats。cpu_percent(interval=0.1) 阻塞 100ms 采样 → 丢线程池,别卡事件循环
    # (性能 P0:此前直接在循环上跑,每次 /stats 请求停摆 100ms 影响所有并发含流式)。
    cpu_pct = await asyncio.to_thread(psutil.cpu_percent, 0.1)
    cpu_per_core = psutil.cpu_percent(interval=0, percpu=True)  # interval=0 非阻塞,即时返回

    # Memory stats
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # Disk stats
    disk = psutil.disk_usage("/")

    # Uptime
    uptime_seconds = time.time() - psutil.boot_time()

    # Top processes。process_iter 遍历全主机进程 → 丢线程池,别在事件循环上扫(性能 P0)。
    # spec process-net-traffic §2.2:读采集器文件 + 扫进程都在同一个线程里,不占事件循环。
    def _procs_with_net():
        net, probe = _read_net_by_pid()
        return _top_processes(net=net if probe["available"] else None), probe

    processes, net_probe = await asyncio.to_thread(_procs_with_net)

    # pinned / stash RAM 聚合(spec ram-pinned-linkage PR-1b):各 runner 经 Pong 上报本进程
    # 的 pinned(含流式预 pin)+ stash 池字节;主进程本体也算上(主进程模型/组件若 stash)。
    # 答「host RAM 去哪了」—— 流式预 pin ~35G 历史上对面板隐身。best-effort,失败不挡 stats。
    pinned_ram_mb = 0
    stash_ram_mb = 0
    for sup in getattr(request.app.state, "runner_supervisors", []) or []:
        pinned_ram_mb += int(getattr(sup, "pinned_ram_mb", 0) or 0)
        stash_ram_mb += int(getattr(sup, "stash_ram_mb", 0) or 0)
    try:
        from src.services.inference.pinned_stash import total_pinned_bytes
        pinned_ram_mb += total_pinned_bytes() // (1024 * 1024)
        if model_mgr is not None and hasattr(model_mgr, "stash_ram_bytes"):
            stash_ram_mb += model_mgr.stash_ram_bytes() // (1024 * 1024)
    except Exception:  # noqa: BLE001 — 主进程口径 best-effort
        pass

    return {
        "gpus": {"count": len(gpus), "gpus": gpus},
        "system": {
            "cpu_usage_percent": cpu_pct,
            "cpu_count": psutil.cpu_count(),
            "cpu_per_core": cpu_per_core,
            "memory_total_gb": round(mem.total / 1024**3, 1),
            "memory_used_gb": round(mem.used / 1024**3, 1),
            "memory_available_gb": round(mem.available / 1024**3, 1),
            "swap_total_gb": round(swap.total / 1024**3, 1),
            "swap_used_gb": round(swap.used / 1024**3, 1),
            "disk_total_gb": round(disk.total / 1024**3, 1),
            "disk_used_gb": round(disk.used / 1024**3, 1),
            "disk_percent": disk.percent,
            # host RAM 锁页/待命占用(MB)—— pinned 不可换页,stash 是 RAM 待命模型(命中秒回)。
            "pinned_ram_mb": pinned_ram_mb,
            "stash_ram_mb": stash_ram_mb,
        },
        "processes": processes,
        "net_probe": net_probe,
        "uptime_seconds": int(uptime_seconds),
    }


class KillProcessRequest(BaseModel):
    pid: int


@router.post("/kill-process", dependencies=[Depends(require_admin)])
async def kill_gpu_process(req: KillProcessRequest, request: Request):
    """Kill an orphan GPU process by PID. Admin only — can disrupt any GPU worker."""
    pid = req.pid

    # 1. Verify PID is in GPU process list
    gpu_procs = _gpu_processes()
    all_gpu_pids = {p["pid"] for procs in gpu_procs.values() for p in procs}
    if pid not in all_gpu_pids:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=404,
            content={"detail": f"PID {pid} not found in GPU process list"},
        )

    # 2. Verify not managed by ModelManager
    model_mgr = getattr(request.app.state, "model_manager", None)
    if model_mgr is not None:
        pid_map = model_mgr.get_pid_map()
        if pid in pid_map:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=409,
                content={"detail": f"PID {pid} is managed model '{pid_map[pid]}'. Use the unload API instead."},
            )

    # 3. Kill with SIGTERM, fallback to SIGKILL. safe_kill refuses pid<=1/0
    #    (broadcast guard). pid is already validated to be in the GPU-process list
    #    (step 1) and not a managed model (step 2), so it's a real GPU worker.
    from src.services.safe_signal import safe_kill
    try:
        if not safe_kill(pid, signal.SIGTERM):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=400,
                                content={"detail": f"Refused to signal pid {pid} (guard)"})
        logger.info("Sent SIGTERM to GPU process %d", pid)
        # Wait for process to exit — await(非 time.sleep),否则最多 5s 阻塞整个
        # 事件循环,期间所有请求 / WS 心跳 / runner ping 全卡(round2 低)。
        for _ in range(10):
            await asyncio.sleep(0.5)
            if not safe_kill(pid, 0):  # 0 = liveness probe; False → gone
                return {"killed": True, "pid": pid}
        # Still alive, force kill
        safe_kill(pid, signal.SIGKILL)
        logger.info("Sent SIGKILL to GPU process %d", pid)
        return {"killed": True, "pid": pid}
    except ProcessLookupError:
        return {"killed": True, "pid": pid}  # Already dead
    except Exception as e:
        logger.warning("Failed to kill process %d: %s", pid, e)
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed to kill process {pid}: {e}"},
        )


@router.get("/runners")
async def list_runners(request: Request) -> dict:
    """Per-runner 状态，给前端 TaskPanel 的 Buildkite 风 runner 泳道用。

    数据源 = RunnerSupervisor.health_snapshot()（Lane H Task 4）+
    LLMRunner.health_snapshot()（Lane K：让前端用同一份列表渲染 image/tts/llm
    全部 runner 泳道）。Lane K lifespan 填充；未启用时返回空列表。
    """
    supervisors = getattr(request.app.state, "runner_supervisors", [])
    runners = [s.health_snapshot() for s in supervisors]
    llm = getattr(request.app.state, "llm_runner", None)
    if llm is not None:
        runners.append(llm.health_snapshot())
    return {"runners": runners}


@router.get("/usage/summary")
async def usage_summary():
    """Get aggregated usage stats for dashboard."""
    from src.services.usage_service import get_usage_summary
    return await get_usage_summary()


@router.get("/usage/by-model")
async def usage_by_model(since: str | None = None):
    """Get per-model usage breakdown."""
    from src.services.usage_service import get_usage_by_model
    from datetime import datetime
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            pass
    return await get_usage_by_model(since=since_dt)


@router.get("/usage/inference")
async def usage_inference(
    start: str | None = None,
    end: str | None = None,
    interval: str = "day",
    group_by: str = "Model",
    instance_id: int | None = None,
    model: str | None = None,
    format: str = "json",
):
    """Ark-style inference usage query.

    - interval: day | hour
    - group_by: Model | Instance | ApiKey
    - format: json (default, idiomatic list) | columnar (Ark Fields+Data)
    """
    from src.services.usage_service import get_inference_usage
    from datetime import datetime

    def _parse(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None

    return await get_inference_usage(
        start=_parse(start),
        end=_parse(end),
        interval=interval,
        group_by=group_by,
        instance_id=instance_id,
        model=model,
        columnar=(format == "columnar"),
    )
