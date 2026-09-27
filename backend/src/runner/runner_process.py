"""TTS runner 子进程入口 + 内部双 asyncio task.

spec §4.4 / D9：runner 子进程内跑两个 task：
  * pipe-reader —— 持续读 pipe：RunNode 入内部 asyncio.Queue；Abort 置对应
    task 的 threading.Event；LoadModel/UnloadModel/Ping 直接处理。永不阻塞在
    adapter 上 —— 这样 Abort 才能立即置位。
  * node-executor —— 从队列取 RunNode、ModelManager.get_or_load adapter、
    调 adapter.infer (可选传 progress_callback + cancel_flag，按签名探测)、
    发 NodeProgress / NodeResult。

cancel 信号用 threading.Event：adapter 的推理可能在 to_thread 里跑，跨线程
信号必须用 threading 原语（spec §4.4 关键性质 D14）。adapter 的 `infer` 是否接
progress_callback / cancel_flag 由 node-executor 按 signature 探测决定。

Lane D：每个 runner 子进程持有独立 ModelManager（spec §4.5）。LoadModel /
UnloadModel / RunNode 全走 ModelManager。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
import threading
import time
import uuid
from typing import Any

from src.runner import protocol as P
from src.runner.pipe_channel import PipeChannel


def _init_runner_logging(group_id: str) -> None:
    """让 runner 子进程的 INFO 日志可见(统一进父进程日志)。

    runner 由 mp spawn 起,不继承父进程的 logging handler → `logger.info` 默认无 handler
    丢弃,只有库直接写 stdout 的(tqdm 进度 / import 警告)能看到。这让 runner 里发生的
    模型加载 / 组件 L1 命中 / 驱逐等 INFO 全部隐身(用户「日志不统一管理」的一环)。

    runner stdout 已被父进程重定向进同一日志文件,这里配一个 INFO StreamHandler 到 stdout
    即可让 runner 日志汇入。前缀 [runner:<group>] 便于和主进程日志区分。等级可经
    NOUS_RUNNER_LOG_LEVEL 覆盖(默认 INFO)。幂等:已配过 handler 就不重复加。
    """
    root = logging.getLogger()
    if any(getattr(h, "_nous_runner", False) for h in root.handlers):
        return
    level = getattr(logging, os.getenv("NOUS_RUNNER_LOG_LEVEL", "INFO").upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler._nous_runner = True  # type: ignore[attr-defined]  # 幂等标记
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s %(levelname)-5s [runner:{group_id}] %(name)s — %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)


class _RunnerState:
    """runner 子进程内的可变状态。"""

    def __init__(
        self,
        runner_id: str,
        group_id: str,
        gpus: list[int],
        model_manager,  # src.services.model_manager.ModelManager
    ):
        self.runner_id = runner_id
        self.group_id = group_id
        self.gpus = gpus
        # Lane D：真 ModelManager（per-runner 独立实例，spec §4.5）。
        # 替换 Lane C 的极简 dict[model_key -> adapter]。
        self.mm = model_manager
        # 待执行的 RunNode 队列（pipe-reader 投，node-executor 取）
        self.run_queue: asyncio.Queue[P.RunNode] = asyncio.Queue()
        # task_id -> cancel flag（pipe-reader 收 Abort 时 set）
        self.cancel_flags: dict[int, threading.Event] = {}
        # task_id 收到 Abort 但 RunNode 尚未到达 —— RunNode 到达时把这个标记翻成
        # 立即置位的 cancel_flag。覆盖「先 Abort 后 RunNode」 / 「RunNode 还在
        # pipe-reader 队列里就收到 Abort」两种节点边界 cancel 时序（spec §4.4）。
        self.pending_aborts: set[int] = set()
        self.shutdown = asyncio.Event()


def _merge_config_into_spec(state: _RunnerState, model_key: str, config: dict) -> None:
    """把 LoadModel.config 合并进该 model 的 ModelSpec.params。

    ModelSpec frozen —— 用 model_copy(update=...) 不可变更新。真实部署 config
    一般空；这条路径主要服务测试通过 LoadModel 注入 fake 故障开关
    （oom_on_load_count / fail_load / infer_seconds）与 fake 多步数 steps
    （AudioRequest 不带 steps,见 FakeAdapter 的 steps 兜底）。
    """
    if not config:
        return
    spec = state.mm._registry.get(model_key)
    if spec is None:
        return
    merged = {**spec.params, **config}
    state.mm._registry._specs[model_key] = spec.model_copy(
        update={"params": merged}
    )


def _handle_set_model_resident(state: _RunnerState, msg: P.SetModelResident) -> None:
    """SetModelResident → mm.set_model_resident(切 by-key 模型常驻)。同步、不抛 ——
    没加载则 no-op;状态经下个 Pong 快照反映。组件 L1 PR-2c。"""
    try:
        state.mm.set_model_resident(msg.model_id, msg.resident)
    except Exception as e:  # noqa: BLE001
        print(f"[runner_process] set_model_resident failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)


async def _handle_load_model(state: _RunnerState, ch: PipeChannel, msg: P.LoadModel) -> None:
    """LoadModel —— 走 ModelManager.get_or_load（含 OOM evict 重试），发 ModelEvent。"""
    from src.errors import ModelLoadError, ModelNotFoundError

    _merge_config_into_spec(state, msg.model_key, msg.config)
    try:
        await state.mm.get_or_load(msg.model_key)
    except (ModelLoadError, ModelNotFoundError) as e:
        await ch.send_message(P.ModelEvent(
            event="load_failed", model_key=msg.model_key,
            error=f"{type(e).__name__}: {e}",
        ))
        return
    except Exception as e:  # noqa: BLE001 —— 兜底，runner 不崩
        await ch.send_message(P.ModelEvent(
            event="load_failed", model_key=msg.model_key,
            error=f"{type(e).__name__}: {e}",
        ))
        return
    await ch.send_message(P.ModelEvent(event="loaded", model_key=msg.model_key, error=None))


async def _handle_unload_model(state: _RunnerState, ch: PipeChannel, msg: P.UnloadModel) -> None:
    # RAM stash(spec 2026-06-12 PR-2):整模型 adapter 先试 stash(权重挪 CPU 待命,
    # 命中 restore 秒回;Ideogram-4 冷载 95s → 秒级)。不可 stash(组件路线 combo /
    # offload pipe / RAM 水位不足 / 引擎不支持)→ 旧销毁。两条路对外都是「显存已腾」。
    if not await state.mm.stash_model(msg.model_key):
        await state.mm.unload_model(msg.model_key, force=True)
    # 显存就在这个 runner 进程持有的卡上 —— empty_cache 必须在这里跑(主进程跑无效)。
    try:
        import torch  # noqa: PLC0415
        if torch.cuda.is_available():  # type: ignore[attr-defined]
            torch.cuda.empty_cache()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — CI mock torch / 无 GPU 安全跳过
        pass
    await ch.send_message(P.ModelEvent(event="unloaded", model_key=msg.model_key, error=None))


async def _pipe_reader(state: _RunnerState, ch: PipeChannel) -> None:
    """持续读 pipe，分派消息。永不阻塞在 adapter 上。"""
    while not state.shutdown.is_set():
        try:
            msg = await ch.recv_message()
        except ConnectionError:
            # 主进程关了 pipe —— runner 该退出了
            state.shutdown.set()
            return
        except P.ProtocolError:
            # 坏消息，跳过（不崩 runner）
            continue

        if isinstance(msg, P.RunNode):
            flag = threading.Event()
            # 先 Abort 后 RunNode（或 Abort 紧跟 RunNode 但还没 race 完）：
            # pending_aborts 里有同 task_id → 立即置位 flag，让 node-executor
            # 在 dispatch 前 boundary check 直接判 cancelled（spec §4.4）。
            if msg.task_id in state.pending_aborts:
                flag.set()
                state.pending_aborts.discard(msg.task_id)
            state.cancel_flags[msg.task_id] = flag
            state.run_queue.put_nowait(msg)
        elif isinstance(msg, P.Abort):
            flag = state.cancel_flags.get(msg.task_id)
            if flag is not None:
                flag.set()  # node-executor 的 adapter 下一 step 边界看到
            else:
                # Abort 先到 / RunNode 还没到 —— 记下，RunNode 到了再合并
                state.pending_aborts.add(msg.task_id)
        elif isinstance(msg, P.SetModelResident):
            _handle_set_model_resident(state, msg)
        elif isinstance(msg, P.LoadModel):
            await _handle_load_model(state, ch, msg)
        elif isinstance(msg, P.UnloadModel):
            await _handle_unload_model(state, ch, msg)
        elif isinstance(msg, P.Ping):
            # 结构化快照(不只 id):带 source_files/gpu/vram,让主进程把 runner 里的
            # adapter 映射回引擎卡 + 还原系统状态「已加载模型」。
            # host RAM 占用快照(spec ram-pinned-linkage PR-1b):stash 是本进程 RAM stash 池
            # 字节。best-effort,失败不挡 Pong(无 cuda 时 0)。
            try:
                _stash_mb = state.mm.stash_ram_bytes() // (1024 * 1024)
            except Exception:  # noqa: BLE001
                _stash_mb = 0
            await ch.send_message(P.Pong(
                runner_id=state.runner_id,
                loaded_models=state.mm.loaded_models_snapshot(),
                stash_ram_mb=_stash_mb,
            ))
        # 其余消息类型（runner→主进程方向的）不应收到，忽略


def _build_request(node: P.RunNode):
    """按 node_type 构造 typed InferenceRequest。

    spec §3.3：RunNode.node_type 是 runner role —— 现在只有 "tts"(AudioRequest,
    boundary-cancel only)。未知 node_type 抛 ValueError —— node-executor 转成
    NodeResult status=failed。
    """
    from src.services.inference.base import AudioRequest

    if node.node_type == "tts":
        return AudioRequest(
            request_id=f"task-{node.task_id}",
            text=str(node.inputs.get("text", "")),
            voice=str(node.inputs.get("voice", "default")),
            speed=float(node.inputs.get("speed", 1.0) or 1.0),
            sample_rate=int(node.inputs.get("sample_rate", 24000) or 24000),
        )
    raise ValueError(f"unsupported node_type {node.node_type!r} (expected tts)")


async def _node_executor(state: _RunnerState, ch: PipeChannel) -> None:
    """从队列取 RunNode，跑 adapter，发 progress / result。"""
    while not state.shutdown.is_set():
        try:
            node = await asyncio.wait_for(state.run_queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue  # 周期性回头看 shutdown

        cancel_flag = state.cancel_flags.get(node.task_id) or threading.Event()
        started = time.monotonic()

        # 先 build typed request —— 未知 node_type 在这里就判 failed。
        try:
            req = _build_request(node)
        except ValueError as e:
            await ch.send_message(P.NodeResult(
                task_id=node.task_id, node_id=node.node_id, status="failed",
                outputs=None, error=str(e),
                duration_ms=int((time.monotonic() - started) * 1000)))
            state.cancel_flags.pop(node.task_id, None)
            continue

        # adapter 获取:by-key 走 get_or_load(含 OOM evict)。
        try:
            adapter = await state.mm.get_or_load(node.model_key) if node.model_key else None
        except Exception as e:  # noqa: BLE001
            await ch.send_message(P.NodeResult(
                task_id=node.task_id, node_id=node.node_id, status="failed",
                outputs=None, error=f"{type(e).__name__}: {e}",
                duration_ms=int((time.monotonic() - started) * 1000),
            ))
            state.cancel_flags.pop(node.task_id, None)
            continue

        if adapter is None:
            await ch.send_message(P.NodeResult(
                task_id=node.task_id, node_id=node.node_id, status="failed",
                outputs=None, error=f"node {node.node_id!r} has no model_key",
                duration_ms=int((time.monotonic() - started) * 1000),
            ))
            state.cancel_flags.pop(node.task_id, None)
            continue

        # progress_callback —— 每 step 发一个 NodeProgress.
        # 本 Lane fake adapter 在 event loop 里直接调 callback,
        # 用 create_task 排发送即可（Lane G 的真 adapter callback 在 to_thread
        # 工作线程里，那时需改 loop.call_soon_threadsafe）。
        progress_tasks: list[asyncio.Task] = []

        def _on_progress(
            done: int, total: int,
            preview_url: str | None = None,
            *,
            # PR-1a(任务面板重置 L3 进度颗粒度):stage/step/total_steps/step_latency_ms/eta_ms
            # 从 adapter callback 透传到 NodeProgress IPC,再经 workflow_executor → WS → 前端
            # ActiveTaskRow callout 展示「⚡ dit step 27/50 · ETA 5.5s」。
            stage: str | None = None,
            step: int | None = None,
            total_steps: int | None = None,
            step_latency_ms: int | None = None,
            eta_ms: int | None = None,
            progress: float | None = None,
            detail: str | None = None,
            _node=node,
        ) -> None:
            t = asyncio.get_running_loop().create_task(ch.send_message(P.NodeProgress(
                task_id=_node.task_id, node_id=_node.node_id,
                # 显式 progress 优先(callback 自己算了);否则用 done/total。
                progress=progress if progress is not None else (done / total if total else 1.0),
                # 显式 detail 优先;否则 fallback "step n/N"。
                detail=detail if detail is not None else f"step {done}/{total}",
                preview_url=preview_url,  # PR-F:latent 实时 RGB 预览(可选)
                stage=stage,
                # step 默认 = done(1-based);total_steps 默认 = total。
                step=step if step is not None else done,
                total_steps=total_steps if total_steps is not None else total,
                step_latency_ms=step_latency_ms,
                eta_ms=eta_ms,
            )))
            progress_tasks.append(t)

        # in-use 守卫:infer 期间标记 adapter 正在用,unload/evict 绝不卸载它(否则 denoise
        # 在 to_thread 工作线程撞已释放的 CUDA 权重 → segfault)。finally 兜所有退出路径。
        # getattr 兜底:可选安全 hook —— 极简 fake ModelManager(测试 double)可不实现。
        _mark = getattr(state.mm, "mark_adapter_in_use", None)
        _release = getattr(state.mm, "release_adapter", None)
        if _mark is not None:
            _mark(adapter)
        try:
            # PR-1b(2026-05-27 任务面板重置):**统一**用 signature 探测决定是否传
            # progress_callback / cancel_flag。spec §4.4「TTS = boundary-cancel only」
            # 升级 —— TTSEngine.infer 现在接 progress_callback + cancel_flag 可选 kwarg,
            # 发 tts_synth stage 事件(start/end + 支持 streaming 的 engine 还可逐 chunk)。
            # 节点边界 cancel 仍生效(infer 内部 boundary 查 cancel_flag + 仍由 pipe-reader
            # dispatch 前置位)。
            if cancel_flag.is_set():
                raise asyncio.CancelledError()
            infer_params = inspect.signature(adapter.infer).parameters
            infer_kwargs: dict = {}
            if "progress_callback" in infer_params:
                infer_kwargs["progress_callback"] = _on_progress
            if "cancel_flag" in infer_params:
                infer_kwargs["cancel_flag"] = cancel_flag
            result = await adapter.infer(req, **infer_kwargs)
        except ValueError as e:
            # 未知 node_type —— _build_request 抛 ValueError。明确判 failed，
            # 不崩 runner。注意：必须放在泛 except Exception 之前，否则被吞掉、
            # 错误信息里就没有 "node_type" 字样。
            if progress_tasks:
                await asyncio.gather(*progress_tasks, return_exceptions=True)
            await ch.send_message(P.NodeResult(
                task_id=node.task_id, node_id=node.node_id, status="failed",
                outputs=None, error=str(e),
                duration_ms=int((time.monotonic() - started) * 1000),
            ))
            state.cancel_flags.pop(node.task_id, None)
            continue
        except asyncio.CancelledError:
            # 先排空 progress 发送，保证 cancelled NodeResult 在最后
            if progress_tasks:
                await asyncio.gather(*progress_tasks, return_exceptions=True)
            await ch.send_message(P.NodeResult(
                task_id=node.task_id, node_id=node.node_id, status="cancelled",
                outputs=None, error="aborted",
                duration_ms=int((time.monotonic() - started) * 1000),
            ))
            state.cancel_flags.pop(node.task_id, None)
            continue
        except Exception as e:  # noqa: BLE001
            if progress_tasks:
                await asyncio.gather(*progress_tasks, return_exceptions=True)
            # PR-5+:写完整 traceback 到 stderr(runner 子进程的 stderr 走 backend_dev.log)。
            # NodeResult.error 还是简短信息(IPC payload 不塞 huge traceback),但 stderr
            # 有完整 stack,backend log 里 grep 「runner_process traceback」找。
            import traceback as _tb  # noqa: PLC0415
            print(
                f"[runner_process traceback] task={node.task_id} node={node.node_id} type={node.node_type}\n"
                + _tb.format_exc(),
                file=__import__("sys").stderr, flush=True,
            )
            await ch.send_message(P.NodeResult(
                task_id=node.task_id, node_id=node.node_id, status="failed",
                outputs=None, error=f"{type(e).__name__}: {e}",
                duration_ms=int((time.monotonic() - started) * 1000),
            ))
            state.cancel_flags.pop(node.task_id, None)
            continue
        finally:
            # infer 结束(成功/异常/取消):清正在用标记 —— 后续输出处理不碰 adapter CUDA,释放安全。
            if _release is not None:
                _release(adapter)

        # 排空 progress 发送 —— 保证「所有 NodeProgress 先到、NodeResult 后到」
        if progress_tasks:
            await asyncio.gather(*progress_tasks, return_exceptions=True)
        # outputs payload —— adapter 返回的 metadata + media_type(spec §3.3)。
        outputs: dict[str, Any] = {"meta": result.metadata, "media_type": result.media_type}
        await ch.send_message(P.NodeResult(
            task_id=node.task_id, node_id=node.node_id, status="completed",
            outputs=outputs,
            error=None,
            duration_ms=int((time.monotonic() - started) * 1000),
        ))
        state.cancel_flags.pop(node.task_id, None)


async def _runner_loop(state: _RunnerState, ch: PipeChannel) -> None:
    """子进程主协程：发 Ready，起 pipe-reader + node-executor 双 task。"""
    await ch.send_message(P.Ready(
        runner_id=state.runner_id, group_id=state.group_id, gpus=state.gpus,
    ))
    reader = asyncio.create_task(_pipe_reader(state, ch), name="pipe-reader")
    executor = asyncio.create_task(_node_executor(state, ch), name="node-executor")
    await state.shutdown.wait()
    reader.cancel()
    executor.cancel()
    await asyncio.gather(reader, executor, return_exceptions=True)


def runner_main(
    group_id: str,
    gpus: list[int],
    conn: Any,
    *,
    models_yaml_path: str | None = None,
    fake_adapter: bool = False,
) -> None:
    """multiprocessing.Process 的 target —— TTS runner 子进程入口。

    起独立 event loop（spec §4.5：runner 有自己的 Event Loop B）+ 构造 per-runner
    独立 ModelManager（spec §4.5）。fake_adapter=True → 所有模型走 FakeAdapter。
    """
    from src.runner.runner_modelmanager import build_runner_model_manager

    _init_runner_logging(group_id)
    runner_id = f"runner-{group_id}-{uuid.uuid4().hex[:6]}"
    mm = build_runner_model_manager(
        group_id, gpus, models_yaml_path=models_yaml_path, fake_adapter=fake_adapter,
    )
    state = _RunnerState(runner_id, group_id, gpus, mm)
    ch = PipeChannel(conn)
    try:
        asyncio.run(_runner_loop(state, ch))
    finally:
        ch.close()
