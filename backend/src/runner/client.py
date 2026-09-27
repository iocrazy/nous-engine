"""RunnerClient —— 主进程侧、PipeChannel 之上的节点级 RPC（spec §3.5）.

主进程对每个 TTS runner 持一个 RunnerClient。它：
  * start()  —— 起后台 demux 协程，等 runner 的 Ready 握手。
  * run_node(spec, on_progress) —— 发 RunNode，await 到对应的 NodeResult；
    期间的 NodeProgress 路由给 on_progress 回调。
  * load_model / unload_model —— 发消息，await 对应 ModelEvent。
  * ping —— 发 Ping，await Pong（supervisor 的 watchdog 用）。
  * abort(task_id) —— 发 Abort（不等回，runner 会照常发 NodeResult(cancelled)）。

demux：runner 发回的消息全经一个后台协程读，按 task_id / 类型路由到对应的
asyncio.Future 或回调。pipe EOF（runner crash）→ 所有 inflight future 置
ConnectionError 异常。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from src.runner import protocol as P
from src.runner.pipe_channel import PipeChannel

logger = logging.getLogger(__name__)


class RunnerClient:
    def __init__(
        self,
        conn: Any,
        *,
        runner_id: str,
        ready_timeout: float = 30.0,
    ) -> None:
        self._ch = PipeChannel(conn)
        self.runner_id = runner_id
        self._ready_timeout = ready_timeout

        self._ready = asyncio.Event()
        self._connected = True
        self.gpus: list[int] = []
        self.group_id: str | None = None

        # task_id -> Future[NodeResult]
        self._node_futures: dict[int, asyncio.Future] = {}
        # task_id -> on_progress 回调
        self._progress_cbs: dict[int, Callable[[P.NodeProgress], None]] = {}
        # model_key -> Future[bool]（ModelEvent loaded/load_failed）
        self._model_futures: dict[str, asyncio.Future] = {}
        # 单个待回 Pong 的 Future（ping 是串行的，watchdog 一次一个）。
        # Bug 3 PR-2b 起 reconcile 也会 ping → 与 watchdog 可能并发,共享单个
        # _pong_future 会串线(一个 Pong 解错 future)→ 另一个 wait_for 超时 →
        # watchdog 误判 crash 重启。_ping_lock 串行化所有 ping,杜绝该 race。
        self._pong_future: asyncio.Future | None = None
        self._ping_lock = asyncio.Lock()

        # Bug 3 PR-2b:每个 NodeResult(节点跑完)后触发 —— supervisor 用它在 tts
        # 节点完成后立刻 reconcile 已加载快照(此时新 adapter 已写进 runner _models,
        # 比等满 30s watchdog ping 快)。
        self.on_node_done: Callable[[], None] | None = None

        self._demux_task: asyncio.Task | None = None

        # Lane K follow-up: inflight dispatch 状态跟踪 —— 给 /api/v1/monitor/runners
        # 的 current_task 字段供数据，让前端 TaskPanel 真显示「正在跑啥」+ 进度条。
        # task_id -> {task_id, workflow_name, node_id, node_type, started_at, progress, detail}
        self._dispatches: dict[int, dict[str, Any]] = {}

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def current_dispatch(self) -> dict[str, Any] | None:
        """当前 runner 正在执行的任务信息 —— 给 /api/v1/monitor/runners 的 current_task
        字段供数据。多个 inflight 时优先返回有 progress 的(= runner 在它身上发过
        NodeProgress = 正在跑它);都没 progress 则返回最早入队的(probably-next-to-run)。
        无 inflight 返回 None。
        """
        if not self._dispatches:
            return None
        progressing = [d for d in self._dispatches.values() if d.get("progress", 0) > 0]
        if progressing:
            return max(progressing, key=lambda d: d.get("progress", 0))
        return min(self._dispatches.values(), key=lambda d: d.get("started_at", 0))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """起 demux 协程，等 Ready 握手。"""
        self._demux_task = asyncio.create_task(self._demux_loop(), name="runner-demux")
        await asyncio.wait_for(self._ready.wait(), timeout=self._ready_timeout)

    async def close(self) -> None:
        self._connected = False
        if self._demux_task is not None:
            self._demux_task.cancel()
            try:
                await self._demux_task
            except asyncio.CancelledError:
                pass
        # 关键(修任务永久挂起):cancel demux 后,EOF 可能还没被 demux 读到、_fail_all_inflight
        # 没跑过 —— 此时 supervisor._restart 走 ping-timeout 路径调 close(),正等 run_node 的
        # 协程会永久挂起(无超时)。close 主动 fail 所有 inflight future,保证关闭即报错。
        # 已 done 的 future 由 _fail_all_inflight 内部 `if not done` 跳过,不重复置异常。
        self._fail_all_inflight(ConnectionError("runner client closed"))
        self._ch.close()

    def _fail_all_inflight(self, exc: Exception) -> None:
        """runner 断连 —— 所有等待中的 future 置异常 + dispatch 跟踪清空。"""
        for fut in list(self._node_futures.values()):
            if not fut.done():
                fut.set_exception(exc)
        for fut in list(self._model_futures.values()):
            if not fut.done():
                fut.set_exception(exc)
        if self._pong_future is not None and not self._pong_future.done():
            self._pong_future.set_exception(exc)
        self._node_futures.clear()
        self._model_futures.clear()
        self._dispatches.clear()
        # round10:断连时 _progress_cbs 也要清。否则 run_node 的 await fut 被置异常返回,
        # 但对应的 on_progress 回调只在 NodeResult 到达时(line 170)才 pop —— 断连路径
        # 没有 NodeResult → 回调永久残留在 _progress_cbs。每次 runner crash/重启都漏掉
        # 当时 in-flight 任务的回调,长跑进程缓慢累积(闭包还可能持图节点引用)。
        self._progress_cbs.clear()

    # ------------------------------------------------------------------
    # demux
    # ------------------------------------------------------------------

    async def _demux_loop(self) -> None:
        while True:
            try:
                msg = await self._ch.recv_message()
            except ConnectionError as e:
                self._connected = False
                self._fail_all_inflight(e)
                return
            except P.ProtocolError:
                continue  # 坏消息跳过，不崩 demux

            if isinstance(msg, P.Ready):
                self.group_id = msg.group_id
                self.gpus = msg.gpus
                self._ready.set()
            elif isinstance(msg, P.NodeProgress):
                # Lane K follow-up: 把最新 progress 写进 dispatch 状态,/runners 暴露
                d = self._dispatches.get(msg.task_id)
                if d is not None:
                    d["progress"] = msg.progress
                    d["detail"] = msg.detail
                cb = self._progress_cbs.get(msg.task_id)
                if cb is not None:
                    # round6:用户回调异常绝不能逃出 demux loop —— 否则杀 loop,后续所有
                    # NodeResult 没人路由、in-flight + 新 run_node future 永不 resolve、
                    # _connected 仍 True、ping 不 resolve → 拖到 5min 兜底重启才恢复。
                    try:
                        cb(msg)
                    except Exception:  # noqa: BLE001
                        logger.exception("progress callback failed (task %s)", msg.task_id)
            elif isinstance(msg, P.NodeResult):
                fut = self._node_futures.pop(msg.task_id, None)
                self._progress_cbs.pop(msg.task_id, None)
                # Lane K follow-up: dispatch 完成,从跟踪表移除
                self._dispatches.pop(msg.task_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                # Bug 3 PR-2b:节点跑完(新 adapter 已注册进 runner _models)→ 触发
                # supervisor reconcile,让已加载快照即时反映,不等 30s ping。
                if self.on_node_done is not None:
                    try:
                        self.on_node_done()
                    except Exception:  # noqa: BLE001 — 回调异常不杀 demux(见上)
                        logger.exception("on_node_done callback failed")
            elif isinstance(msg, P.ModelEvent):
                fut = self._model_futures.pop(msg.model_key, None)
                if fut is not None and not fut.done():
                    fut.set_result(msg.event == "loaded")
            elif isinstance(msg, P.Pong):
                if self._pong_future is not None and not self._pong_future.done():
                    self._pong_future.set_result(msg)

    # ------------------------------------------------------------------
    # RPC
    # ------------------------------------------------------------------

    async def run_node(
        self,
        spec: P.RunNode,
        *,
        on_progress: Callable[[P.NodeProgress], None] | None = None,
        workflow_name: str = "",
    ) -> P.NodeResult:
        """发 RunNode，await 对应的 NodeResult。

        workflow_name: 给 /api/v1/monitor/runners current_task 显示用的人读字段
        (前端 TaskPanel 泳道挂在 runner 上的「正在跑」名字)。无则空串。
        """
        if not self._connected:
            raise ConnectionError("runner disconnected")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._node_futures[spec.task_id] = fut
        if on_progress is not None:
            self._progress_cbs[spec.task_id] = on_progress
        # Lane K follow-up: 登记 inflight dispatch,供 current_dispatch property 读
        self._dispatches[spec.task_id] = {
            "task_id": spec.task_id,
            "workflow_name": workflow_name,
            "node_id": spec.node_id,
            "node_type": spec.node_type,
            "started_at": time.monotonic(),
            "progress": 0.0,
            "detail": None,
        }
        try:
            await self._ch.send_message(spec)
            return await fut
        finally:
            # 兜底:_fail_all_inflight / NodeResult 都会移除,但异常路径(send_message 抛、
            # 取消)也保证不残留。
            self._dispatches.pop(spec.task_id, None)
            # round10:同理 pop on_progress 回调 —— send_message 抛 / 协程被取消时
            # NodeResult 永不到达,不在这清就会残留(_node_futures 已由 fail-path 兜底,
            # _progress_cbs 之前没有兜底)。
            self._progress_cbs.pop(spec.task_id, None)

    async def load_model(self, model_key: str, *, config: dict | None = None) -> bool:
        """发 LoadModel，await ModelEvent。返回是否加载成功。"""
        if not self._connected:
            raise ConnectionError("runner disconnected")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._model_futures[model_key] = fut
        await self._ch.send_message(P.LoadModel(model_key=model_key, config=config or {}))
        return await fut

    async def set_model_resident(self, model_id: str, resident: bool) -> None:
        """发 SetModelResident —— 切 by-key 模型常驻位,fire-and-forget;走下个 Pong。
        组件 L1 PR-2c。"""
        if not self._connected:
            raise ConnectionError("runner disconnected")
        await self._ch.send_message(P.SetModelResident(model_id=model_id, resident=resident))

    async def unload_model(self, model_key: str) -> None:
        if not self._connected:
            raise ConnectionError("runner disconnected")
        await self._ch.send_message(P.UnloadModel(model_key=model_key))

    async def abort(self, task_id: int, node_id: str | None = None) -> None:
        """发 Abort —— 不等回，runner 会照常发 NodeResult(cancelled)。"""
        if not self._connected:
            return
        await self._ch.send_message(P.Abort(task_id=task_id, node_id=node_id))

    async def ping(self) -> P.Pong:
        """发 Ping，await Pong。supervisor 的 watchdog + PR-2b reconcile 都用 —— 用
        _ping_lock 串行化,避免并发 ping 共享 _pong_future 串线(详见 __init__ 注释)。"""
        if not self._connected:
            raise ConnectionError("runner disconnected")
        async with self._ping_lock:
            if not self._connected:
                raise ConnectionError("runner disconnected")
            loop = asyncio.get_running_loop()
            self._pong_future = loop.create_future()
            await self._ch.send_message(P.Ping())
            return await self._pong_future
