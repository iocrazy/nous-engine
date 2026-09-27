"""ProgressTracker — 统一进度发射器(PR-4,任务面板重置)。

抽出 PR-1a/b/c 三处冗余实现的公共逻辑:
- (已删的)image_modular 的 `_make_emit` + `_step_cb` 每步 latency + ETA(2026-09-26 随自建图像引擎删除)
- tts_engines/base 的 `_make_tts_emit` + `_tts_progress_ticker` 估值
- workflow_executor 的 `_LlmProgressEmitter` token 滑窗 + throttle

统一 API + 公共导出。**第三方包**(`backend/nodes/{pkg}/executor.py`)可 import 复用:

    from src.services.inference.progress_tracker import ProgressTracker

设计要点:
- per-step latency 16-token 滑窗均值(平滑掉首步 cudnn benchmark / cold start spike)
- ETA = (total - done) × avg_latency
- throttle 节流(高速 stream 不打爆 WS)
- stage 切换 / tick 估值 / finish 末帧 三种 emit 时机
- 末帧 progress=1.0 / eta=0,中间帧 progress 上限 0.95(防止 100% 后回退)
- callback signature 兼容:`(done, total, **extras)` → 失败降级 `(done, total)`

设计参考 ComfyUI 的 `comfy.utils.ProgressBar`:统一 API,各 node 只汇报「step n/N」,
latency / ETA / emit 全由 tracker 处理。但 nous 多任务 + 多 lane(image/tts/llm/vision)
所以在 tracker 里加 stage 概念 + 估值 tick 模式(tts ticker 用)。
"""
from __future__ import annotations

import inspect
import time
from typing import Any, Callable

# Callback signature:`(done, total, **extras)` ── 兼容旧 fake 的 `(done, total)`。
ProgressCallback = Callable[..., None]


def emit_to_callback(cb, done: int, total: int, kwarg_candidates: list[dict]) -> None:
    """按候选 kwargs 顺序找**第一个签名能接受**的调用方式,只调一次。

    用 inspect.signature().bind 判定兼容(不触发回调 body),故回调 body 里真抛的
    TypeError **不再被误当签名不符吞掉 + 降级重调**(审查发现的 footgun:签名匹配但
    body 有 bug 抛 TypeError 时,旧写法 `except TypeError` 会把它当降级信号,副作用重复
    执行、真错被掩盖)。所有候选都不匹配 → 退到最简 (done, total)。

    signature 无法内省(C 函数/内建等罕见情形)→ 回退旧的 try/except 探测(接受 footgun)。
    """
    try:
        sig = inspect.signature(cb)
    except (ValueError, TypeError):
        sig = None

    if sig is not None:
        for kwargs in kwarg_candidates:
            try:
                sig.bind(done, total, **kwargs)
            except TypeError:
                continue  # 这个签名不接受这组参数 → 试下一个候选
            cb(done, total, **kwargs)  # 兼容 → 调一次;body 的 TypeError 正常冒泡
            return
        cb(done, total)  # 没有候选匹配 → 最简契约
        return

    # 无法内省签名:退回旧探测(极少见,如内建 C 回调)。
    for kwargs in [*kwarg_candidates, {}]:
        try:
            cb(done, total, **kwargs)
            return
        except TypeError:
            continue


class ProgressTracker:
    """统一进度发射器。

    用法(典型 — image adapter,真 per-step):

        pt = ProgressTracker(progress_callback)
        pt.stage('text_encode')                       # pipe encode 前发一帧
        # ... pipe.callback_on_step_end ...
        def _step_cb(i, ...):
            pt.step(i + 1, total_steps, stage='dit_denoise', preview_url=...)
        # ... pipe() done ...
        pt.stage('vae_decode', progress=1.0)          # 末尾不调 finish:vae_decode 已 progress=1

    用法(tts adapter,无真 step):

        async with ProgressTracker(cb, throttle_ms=300) as pt:
            pt.stage('tts_synth', progress=0)
            # 主线程同时跑 ticker 调 pt.tick(elapsed, est_total)
            result = await asyncio.to_thread(synthesize, ...)
            pt.finish(total_units=int(result.duration_seconds))

    用法(llm,token stream + throttle):

        pt = ProgressTracker(cb, stage='llm_gen', throttle_ms=250, latency_window=16)
        for token in stream:
            count += 1
            pt.step(count, max_tokens)                # throttle 内自动节流
        pt.finish(total_units=usage.completion_tokens)
    """

    PROGRESS_CAP_BEFORE_FINISH: float = 0.95
    """中间帧 progress 上限。finish() 才到 1.0,避免「100% 后回退」UX。"""

    def __init__(
        self,
        callback: ProgressCallback | None,
        *,
        stage: str | None = None,
        throttle_ms: int = 0,
        latency_window: int = 16,
    ) -> None:
        """构造。

        Args:
            callback: progress 回调,signature `(done, total, **extras)`。None → no-op。
            stage: 默认 stage 名(可被 step/tick/finish 的 stage= 参数覆盖)。
            throttle_ms: 中间帧节流间隔(ms)。0 = 不节流(image 真 per-step 用);
                250 = llm token rate;300 = tts ticker。stage()/finish() 不受节流约束。
            latency_window: per-step latency 滑窗大小(取均值算 ETA)。默认 16,
                平滑掉首步冷启动 spike(image cudnn benchmark / llm 首 token)。
        """
        self._callback = callback
        self._default_stage = stage
        self._throttle_ms = max(0, int(throttle_ms))
        self._window = max(1, int(latency_window))
        self._t0 = time.monotonic()
        self._last_step_t = self._t0
        self._last_emit_t = 0.0
        self._latencies: list[int] = []
        self._finished = False

    # --- context manager 语法糖(可选,不强制 finish)---
    def __enter__(self) -> "ProgressTracker":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    # --- public API ---

    @property
    def has_callback(self) -> bool:
        """callback != None。用于跳过昂贵的 preview 计算等。"""
        return self._callback is not None

    def stage(
        self,
        name: str,
        *,
        progress: float = 0.0,
        detail: str | None = None,
        step: int | None = None,
        total_steps: int | None = None,
    ) -> dict[str, Any] | None:
        """切换 stage 帧。不更新 latency 滑窗(stage 切换不是 step 边界)。
        finished 后 no-op,throttle 不约束(stage 切换是关键事件,必须发)。
        返发射的 payload(供 async caller 自己 await 派,callback=None 模式用)。"""
        if self._finished:
            return None
        return self._emit(
            done=step or 0,
            total=total_steps or 0,
            stage=name,
            progress=progress,
            detail=detail or name,
            step_latency_ms=None,
            eta_ms=None,
        )

    def step(
        self,
        done: int,
        total: int,
        *,
        stage: str | None = None,
        preview_url: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any] | None:
        """1 步进度。算 latency 滑窗 + ETA + throttle。

        滑窗:最近 `latency_window` 步的均值。
        ETA:`(total - done) × avg_latency`。
        Throttle:小于 `throttle_ms` 不发(末帧 finish 不受约束)。
        progress:`min(0.95, done/total)`,留 finish 拉到 1.0。
        返发射的 payload(throttle 跳过返 None;callback=None 模式 caller 用返值 async emit)。
        """
        if self._finished:
            return None
        now = time.monotonic()
        delta_ms = int((now - self._last_step_t) * 1000)
        self._last_step_t = now
        self._latencies.append(delta_ms)
        if len(self._latencies) > self._window:
            self._latencies.pop(0)
        # throttle 检查
        if self._throttle_ms > 0 and (now - self._last_emit_t) * 1000 < self._throttle_ms:
            return None
        self._last_emit_t = now
        avg = int(sum(self._latencies) / len(self._latencies)) if self._latencies else 0
        eta_ms = max(0, avg * max(0, total - done))
        stage_name = stage or self._default_stage
        progress = (
            min(self.PROGRESS_CAP_BEFORE_FINISH, done / total) if total > 0 else 0.0
        )
        return self._emit(
            done=done, total=total,
            stage=stage_name,
            progress=progress,
            detail=detail or (f"{stage_name} {done}/{total}" if stage_name else None),
            step_latency_ms=avg,
            eta_ms=eta_ms,
            preview_url=preview_url,
        )

    def tick(
        self,
        elapsed_s: float,
        estimated_total_s: float,
        *,
        stage: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any] | None:
        """估值 tick 模式。无真 step 边界(tts ticker 用):基于 elapsed / estimated_total
        算 progress + eta。不更新 latency 滑窗(没有真 step latency)。
        返发射的 payload(throttle 跳过返 None)。"""
        if self._finished:
            return None
        now = time.monotonic()
        if self._throttle_ms > 0 and (now - self._last_emit_t) * 1000 < self._throttle_ms:
            return None
        self._last_emit_t = now
        progress = (
            min(self.PROGRESS_CAP_BEFORE_FINISH, elapsed_s / estimated_total_s)
            if estimated_total_s > 0 else 0.0
        )
        eta_ms = max(0, int((estimated_total_s - elapsed_s) * 1000))
        stage_name = stage or self._default_stage
        done = max(0, int(round(elapsed_s)))
        total = max(1, int(round(estimated_total_s)))
        return self._emit(
            done=done, total=total,
            stage=stage_name,
            progress=progress,
            detail=detail or (f"{stage_name} {done}/{total}s" if stage_name else None),
            step_latency_ms=self._throttle_ms or None,
            eta_ms=eta_ms,
        )

    def finish(
        self,
        total_units: int,
        *,
        stage: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any] | None:
        """末帧:`progress=1.0` / `eta=0` / `step=total_units`。
        finished 后调用是 no-op。throttle 不约束。返发射的 payload。"""
        if self._finished:
            return None
        self._finished = True
        latency_ms = int((time.monotonic() - self._t0) * 1000)
        stage_name = stage or self._default_stage
        return self._emit(
            done=total_units, total=total_units,
            stage=stage_name,
            progress=1.0,
            detail=detail or (f"{stage_name} done" if stage_name else "done"),
            step_latency_ms=latency_ms,
            eta_ms=0,
        )

    # --- private ---

    def _emit(
        self,
        *,
        done: int,
        total: int,
        stage: str | None,
        progress: float,
        detail: str | None,
        step_latency_ms: int | None,
        eta_ms: int | None,
        preview_url: str | None = None,
    ) -> dict[str, Any]:
        """调 callback,做兼容降级 — 老 fake 只接 `(done, total)` / `(done, total, preview_url)`。
        返发射的 payload(供 callback=None 模式 caller 自己异步派,见 LLM emitter)。"""
        extras: dict[str, Any] = {
            "stage": stage,
            "progress": progress,
            "detail": detail,
            "step_latency_ms": step_latency_ms,
            "eta_ms": eta_ms,
        }
        if preview_url is not None:
            extras["preview_url"] = preview_url
        payload = {"done": done, "total": total, **extras}
        cb = self._callback
        if cb is None:
            return payload
        # 候选签名(富 → 简):full extras → 只 preview_url(老 PR-F 契约)→ (done,total)。
        # emit_to_callback 用 signature.bind 判兼容,不会把回调 body 的真 TypeError 当降级信号。
        emit_to_callback(cb, done, total, [extras, {"preview_url": preview_url}])
        return payload
