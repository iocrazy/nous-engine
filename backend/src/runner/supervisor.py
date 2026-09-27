"""RunnerSupervisor —— 主进程侧的 runner 子进程监管（spec §4.2）.

职责：
  * start()  —— fork runner 子进程，建 RunnerClient，等 Ready 握手。
  * watchdog —— 每 ping_interval 发一次 ping；ping_timeout 内无 Pong 或 pipe
    EOF → 判定 crash → _restart()。
  * _restart() —— 终结旧 runner（terminate → kill）；inflight task 全标 failed
    (runner_crashed)；按 RESTART_BACKOFF 退避；过 F2 GPU-free gate（轮询
    gpu_free_probe 直到该 group 的 GPU 显存回落）；重新 fork + 握手。
  * 成功跑满 stable_seconds（默认 30min）后 reset restart_count（spec §4.2 第 6 步）。

本 Lane 用 fake runner（adapter_class 默认 FakeAdapter）+ 注入式 gpu_free_probe
（不碰真 nvidia-smi）。生产环境 Lane H 接真探针 + resident preload。
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import time
from typing import Callable

from src.runner.client import RunnerClient
from src.runner.runner_process import runner_main

logger = logging.getLogger(__name__)

# spec §4.2 默认值
DEFAULT_PING_INTERVAL = 30.0
DEFAULT_PING_TIMEOUT = 10.0
DEFAULT_RESTART_BACKOFF = [5.0, 15.0, 60.0, 300.0]  # 封顶 5 min
DEFAULT_STABLE_SECONDS = 30 * 60  # 跑满 30min 视为稳定，reset crash count
# ping 超时但进程仍活 = 事件循环被长 dispatch 阻塞(忙),不是 crash —— 容忍。仅当
# 持续无响应超过此阈值(疑死锁;没有图/音生成要 5min)才兜底重启。见 _watchdog。
DEFAULT_MAX_UNRESPONSIVE_SECONDS = 300.0

_SPAWN = mp.get_context("spawn")  # CUDA 子进程惯例：spawn 不 fork


def _default_gpu_free_probe(gpus: list[int]) -> bool:
    """生产用 GPU-free 探针：nvidia-smi 查这些 GPU 的显存是否回落到基线（spec 4.2 F2）。

    Lane H 把 Lane C 的 `return True` 骨架换成真实现 —— 委托
    `gpu_free_probe.make_gpu_free_probe()`（默认基线 = 每卡 total 的 80%）。
    无 GPU 环境下该探针仍保守返回 True，不阻塞重启。
    """
    from src.runner.gpu_free_probe import make_gpu_free_probe
    return make_gpu_free_probe()(gpus)


class RunnerSupervisor:
    def __init__(
        self,
        *,
        group_id: str,
        gpus: list[int],
        models_yaml_path: str | None = None,
        fake_adapter: bool = False,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        ping_timeout: float = DEFAULT_PING_TIMEOUT,
        restart_backoff: list[float] | None = None,
        stable_seconds: float = DEFAULT_STABLE_SECONDS,
        max_unresponsive_seconds: float = DEFAULT_MAX_UNRESPONSIVE_SECONDS,
        gpu_free_probe: Callable[[list[int]], bool] | None = None,
        gpu_free_poll_interval: float = 2.0,
        on_task_failed: Callable[[int, str], None] | None = None,
    ) -> None:
        self.group_id = group_id
        self.gpus = gpus
        # Lane D: 替换 adapter_class —— 模型用哪个 adapter 由 ModelSpec.adapter_class
        # （yaml）决定；supervisor 只传「fake 测试模式」开关 + yaml 路径。
        self.models_yaml_path = models_yaml_path
        self.fake_adapter = fake_adapter
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.restart_backoff = restart_backoff or DEFAULT_RESTART_BACKOFF
        self.stable_seconds = stable_seconds
        self.max_unresponsive_seconds = max_unresponsive_seconds
        # ping 超时但进程活着时,记首次无响应的时刻;持续超 max_unresponsive_seconds 才重启。
        # 成功 ping(或重启)清零。
        self._unresponsive_since: float | None = None
        self._gpu_free_probe = gpu_free_probe or _default_gpu_free_probe
        self._gpu_free_poll_interval = gpu_free_poll_interval
        self._on_task_failed = on_task_failed

        self._process: mp.Process | None = None
        self.client: RunnerClient | None = None
        self.restart_count = 0
        # runner 子进程上报的已加载 adapter 快照(每个 ping 对账一次)。主进程的
        # 系统状态「已加载模型」、引擎库 loaded 视图聚合这份 —— tts adapter 真加载在
        # runner 进程,主进程 _models 看不到,这是唯一可见窗口。
        self.loaded_models: list[dict] = []
        # 本 runner 进程的 host RAM stash 池占用(MB,Pong 上报;spec ram-pinned-linkage
        # PR-1b)。/monitor/stats 聚合。
        self.stash_ram_mb: int = 0
        self._reconcile_inflight = False  # PR-2b:去重并发 node-done reconcile
        self._inflight: set[int] = set()
        self._last_spawn_at = 0.0
        self._watchdog_task: asyncio.Task | None = None
        self._stopping = False
        self._restarted_count = 0

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return (
            self._process is not None
            and self._process.is_alive()
            and self.client is not None
            and self.client.is_connected
        )

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def backoff_for(self, restart_index: int) -> float:
        """第 restart_index 次重启该等多久（封顶 restart_backoff 最后一个值）。"""
        if restart_index < len(self.restart_backoff):
            return self.restart_backoff[restart_index]
        return self.restart_backoff[-1]

    def health_snapshot(self) -> dict:
        """给 /health + /api/v1/monitor/runners 端点用的 runner 状态快照。

        纯读现有属性，无副作用。Lane I Dashboard 用 `running` / `restart_count`
        判断 degraded banner + 「重启中 N/M」；current_task(Lane K follow-up)
        让前端 TaskPanel 真显示「正在跑啥」+ 进度(spec §6.1 DD3)。
        """
        current = self.client.current_dispatch if self.client is not None else None
        return {
            "group_id": self.group_id,
            "gpus": list(self.gpus),
            "running": self.is_running,
            # supervisor 子进程是 eager-spawn 的,该在跑 → not running 即不健康(crashed /
            # mid-restart)。与 LLMRunner(IDLE 待命也算健康)的 healthy 语义区分,/health
            # 统一读 healthy 判 degraded。
            "healthy": self.is_running,
            "restart_count": self.restart_count,
            "pid": self.pid,
            "current_task": current,
        }

    # ------------------------------------------------------------------
    # inflight task 登记（crash 时全标 failed）
    # ------------------------------------------------------------------

    def register_inflight(self, task_id: int) -> None:
        self._inflight.add(task_id)

    def unregister_inflight(self, task_id: int) -> None:
        self._inflight.discard(task_id)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await self._spawn()
        self._watchdog_task = asyncio.create_task(
            self._watchdog(), name=f"watchdog-{self.group_id}"
        )
        # 立即对账一次,别让 UI 等满第一个 ping_interval(默认 30s)才看到已加载模型。
        await self._reconcile_loaded()

    def _schedule_reconcile(self) -> None:
        """on_node_done 同步回调 —— 调度一次异步 reconcile,不阻塞 demux。去重:已有
        一次在飞就不再排(快连多个节点不堆 ping)。无 running loop(测试)静默跳过。"""
        if self._stopping or self._reconcile_inflight:
            return
        try:
            asyncio.get_running_loop().create_task(self._reconcile_loaded())
        except RuntimeError:
            pass

    async def _reconcile_loaded(self) -> None:
        """ping 一次,把 runner 上报的已加载 adapter 快照存进 self.loaded_models。
        best-effort —— ping 失败/超时不抛(crash 检测是 watchdog 的职责,这里只刷状态)。"""
        if self.client is None or not self._connected_for_reconcile():
            return
        self._reconcile_inflight = True
        try:
            pong = await asyncio.wait_for(self.client.ping(), timeout=self.ping_timeout)
            self.loaded_models = list(pong.loaded_models or [])
            self.stash_ram_mb = int(getattr(pong, "stash_ram_mb", 0) or 0)
        except Exception:  # noqa: BLE001 — 状态刷新失败不影响监管主流程
            pass
        finally:
            self._reconcile_inflight = False

    def _connected_for_reconcile(self) -> bool:
        return self.client is not None and self.client.is_connected

    async def _spawn(self) -> None:
        """fork runner 子进程 + 建 client + 等 Ready。"""
        parent_conn, child_conn = _SPAWN.Pipe()
        proc = _SPAWN.Process(
            target=runner_main,
            args=(self.group_id, self.gpus, child_conn),
            kwargs={
                "models_yaml_path": self.models_yaml_path,
                "fake_adapter": self.fake_adapter,
            },
            daemon=True,
            name=f"runner-{self.group_id}",
        )
        proc.start()
        child_conn.close()  # 主进程侧不用 child 端
        self._process = proc
        self.client = RunnerClient(parent_conn, runner_id=f"runner-{self.group_id}")
        # Bug 3 PR-2b:节点跑完即时刷新已加载快照(新 adapter 已进 runner _models)。
        self.client.on_node_done = self._schedule_reconcile
        try:
            await self.client.start()  # 等 Ready 握手
        except BaseException:
            # round10:握手失败/取消 —— 子进程已 fork(line 上 proc.start()),占着 GPU。
            # 初始 start() 路径此时 watchdog 还没建(start() line 156 在 _spawn 之后),
            # 没人回收 → orphan runner 永久泄漏显存。_restart 路径虽下轮会 _terminate,但
            # 让 _spawn 自己清更稳。主动终结进程 + 关 client channel 再抛。
            await self._terminate_process()
            await self.client.close()
            raise
        self._last_spawn_at = time.monotonic()
        logger.info(
            "runner %s spawned (pid=%s, gpus=%s)", self.group_id, proc.pid, self.gpus
        )

    async def stop(self) -> None:
        """优雅停止 —— 不再重启，终结子进程。"""
        self._stopping = True
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        if self.client is not None:
            await self.client.close()
        await self._terminate_process()

    async def _terminate_process(self) -> None:
        """SIGTERM 5s → SIGKILL。"""
        proc = self._process
        if proc is None:
            return
        if proc.is_alive():
            proc.terminate()
            for _ in range(50):  # 最多等 5s
                if not proc.is_alive():
                    break
                await asyncio.sleep(0.1)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=3.0)
        else:
            proc.join(timeout=1.0)

    # ------------------------------------------------------------------
    # watchdog + restart
    # ------------------------------------------------------------------

    async def _watchdog(self) -> None:
        """每 ping_interval ping 一次；超时 / EOF → crash → 重启。"""
        while not self._stopping:
            await asyncio.sleep(self.ping_interval)
            if self._stopping:
                return
            try:
                pong = await asyncio.wait_for(self.client.ping(), timeout=self.ping_timeout)
                # 顺手对账已加载快照(ping 本就为存活检测,Pong 带回了状态,别丢)。
                self.loaded_models = list(pong.loaded_models or [])
                self._unresponsive_since = None  # 答上了 → 恢复
            except ConnectionError:
                # pipe EOF —— 子进程真死了(crash/OOM kill/segfault),立刻重启。
                if self._stopping:
                    return
                logger.warning("runner %s pipe EOF (crashed) -> restarting", self.group_id)
                self._unresponsive_since = None
                await self._restart()
            except asyncio.TimeoutError:
                # ping 超时 ——「忙」还是「死」?子进程里 pipe-reader 与 node-executor 同一
                # 事件循环,长 dispatch(adapter.infer 阻塞)会让 pipe-reader 答不上 ping。
                # 进程还活着 = 忙,不是 crash —— **绝不能 kill 一个正在出图的 runner**
                # (这正是 anima/长 denoise 被误杀的根因)。只有进程真死、或持续无响应过久
                # (疑死锁,没有图/音生成要 5min)才重启。
                if self._stopping:
                    return
                alive = self._process is not None and self._process.is_alive()
                if not alive:
                    logger.warning("runner %s ping timeout + process dead -> restarting", self.group_id)
                    self._unresponsive_since = None
                    await self._restart()
                    continue
                now = time.monotonic()
                if self._unresponsive_since is None:
                    self._unresponsive_since = now
                stuck_for = now - self._unresponsive_since
                if stuck_for >= self.max_unresponsive_seconds:
                    logger.warning(
                        "runner %s unresponsive %.0fs while alive (疑死锁) -> restarting",
                        self.group_id, stuck_for,
                    )
                    self._unresponsive_since = None
                    await self._restart()
                else:
                    logger.debug(
                        "runner %s ping timeout but process alive (busy dispatch, %.0fs) — tolerating",
                        self.group_id, stuck_for,
                    )

    async def _restart(self) -> None:
        """spec §4.2 crash 检测 + 重启 6 步。"""
        # 1. 终结旧 runner
        await self._terminate_process()
        if self.client is not None:
            await self.client.close()
        # 旧 runner 的已加载 adapter 随进程一起没了,清空快照(respawn 后 _spawn 末尾
        # 不会自动 reconcile —— 等下一个 watchdog ping 重新填,期间显示为空是正确的)。
        self.loaded_models = []
        self.stash_ram_mb = 0  # runner 进程没了,其 stash RAM 随之释放
        self._unresponsive_since = None

        # 2. inflight task 全标 failed (runner_crashed)，不重试
        for task_id in list(self._inflight):
            if self._on_task_failed is not None:
                self._on_task_failed(task_id, "runner_crashed")
        self._inflight.clear()

        # 3. backoff（防 crash 风暴）
        backoff = self.backoff_for(self.restart_count)
        await asyncio.sleep(backoff)

        # 4. F2 GPU-free gate —— 轮询直到该 group 的 GPU 显存回落
        while not self._stopping and not self._gpu_free_probe(self.gpus):
            logger.info(
                "runner %s GPU-free gate: GPUs %s not yet free, waiting",
                self.group_id, self.gpus,
            )
            await asyncio.sleep(self._gpu_free_poll_interval)
        if self._stopping:
            return

        # 5. 重新 fork + 握手
        try:
            await self._spawn()
        except Exception:
            logger.exception("runner %s respawn failed", self.group_id)
            self.restart_count += 1
            return

        # 6. restart_count 累加；成功跑满 stable_seconds 后由
        #    _maybe_reset_crash_count reset（生产由 Lane H 健康检查 loop 调）
        self.restart_count += 1
        self._restarted_count += 1
        logger.info(
            "runner %s restarted (count=%d, backoff=%.1fs)",
            self.group_id, self.restart_count, backoff,
        )

    def _maybe_reset_crash_count(self) -> None:
        """跑满 stable_seconds 无 crash → reset restart_count（spec §4.2 第 6 步）.

        由调用方（Lane H 的健康检查 loop）周期性调用，或 watchdog 每轮调。
        """
        if (
            self.restart_count > 0
            and self._last_spawn_at > 0
            and time.monotonic() - self._last_spawn_at >= self.stable_seconds
        ):
            logger.info(
                "runner %s stable for %.0fs -> reset crash count",
                self.group_id, self.stable_seconds,
            )
            self.restart_count = 0

    # ------------------------------------------------------------------
    # 测试辅助
    # ------------------------------------------------------------------

    async def wait_restarted(self, count: int = 1) -> None:
        """等到 supervisor 完成至少 count 次重启（测试用）。"""
        while self._restarted_count < count:
            await asyncio.sleep(0.05)
