from __future__ import annotations

import asyncio
import importlib
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from src.errors import ModelLoadError, ModelNotFoundError
from src.services.inference.base import InferenceAdapter
from src.services.inference.registry import ModelRegistry, ModelSpec
from src.services.gpu_allocator import GPUAllocator

logger = logging.getLogger(__name__)


# Re-export so existing `from src.services.model_manager import
# ModelLoadError, ModelNotFoundError` keeps working (these moved to
# src.errors so they share the NousError envelope path).
__all__ = ["ModelLoadError", "ModelManager", "ModelNotFoundError"]


class _Placement(BaseModel):
    """一次 load 的落卡结论(ModelManager._resolve_placement 的返回值)。"""

    gpu_indices: list[int] = Field(default_factory=list)   # [] = 未知(加载后探测)
    detect_after_load: bool = False
    reserved_single: tuple[int, int] = (-1, 0)             # (gpu, mb) —— 单卡在途预留
    reserved_group: tuple[list[int], int] | None = None    # (cards, mb/卡) —— 组在途预留

    @property
    def gpu_index(self) -> int:
        return self.gpu_indices[0] if self.gpu_indices else (0 if self.detect_after_load else -1)


class LoadedModel(BaseModel):
    """Runtime entry per loaded model: spec + adapter instance + GPU placement
    + LRU bookkeeping. Mutable (touch() updates last_used)."""

    spec: ModelSpec
    adapter: InferenceAdapter
    gpu_index: int  # primary GPU (for single-GPU models)
    gpu_indices: list[int] = Field(default_factory=list)  # all GPUs (for tensor-parallel)
    loaded_at: float = Field(default_factory=time.monotonic)
    last_used: float = Field(default_factory=time.monotonic)
    # RAM stash(spec 2026-06-12 PR-2):整模型 adapter 权重已挪 CPU 待命(不占显存,
    # 命中 restore 秒回)。
    stashed: bool = False

    # InferenceAdapter is a non-pydantic ABC instance; allow as field value.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def cards(self) -> list[int]:
        """这个模型占着哪几张卡 —— 「gpu_indices 优先、退回主卡」这条回退逻辑的唯一实现
        (evict_lru 等处共用)。"""
        idxs = [int(i) for i in (self.gpu_indices or []) if i is not None and i >= 0]
        if idxs:
            return idxs
        return [self.gpu_index] if self.gpu_index is not None and self.gpu_index >= 0 else []


class ModelManager:
    """Unified model lifecycle manager: load, unload, evict, reference-count."""

    def __init__(self, registry: ModelRegistry, allocator: GPUAllocator) -> None:
        self._registry = registry
        self._allocator = allocator
        self._models: dict[str, LoadedModel] = {}
        self._references: dict[str, set[str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # 正在 infer 的 model_id —— node-executor 在 adapter.infer 期间标记。unload/evict
        # 绝不卸载在用的(即使 force):denoise 在 to_thread 工作线程跑,卸载 = 释放正在用的
        # CUDA 权重 → 工作线程撞已释放显存 → segfault。
        # model_id → 并发 infer 计数(round-review C5:原为 set,同一 adapter 两个并发
        # infer 时第一个结束就 discard 清标记、第二个还在 to_thread 跑 → evict/unload 放行 →
        # segfault)。改引用计数:减到 0 才移除 key。`mid in self._in_use` 语义不变(== 计数>0)。
        self._in_use: dict[str, int] = {}
        # Per-model load failures (set by background preload tasks or prior
        # failed load_model attempts). get_loaded_adapter raises ModelLoadError
        # when a record exists. Cleared on successful load.
        self._load_failures: dict[str, str] = {}
        # 全局加载串行门(2026-07-06 生产事故:开机 _load_wf_deps 与 preload_residents 是
        # 两个独立 asyncio task,同一瞬间往同一张卡 spawn 两个 vLLM → engine core init
        # 竞争,embedding 加载失败;跨卡则是相关联功率尖峰,GPU 掉总线诱因)。选卡由
        # allocator 的在途预留保证并发安全;这里串行化真正的 adapter.load(),一次只加载
        # 一个模型 —— 加载变顺序(略慢但稳),消除同卡 init 竞争 + 多卡功率齐冲。
        #
        # 2026-09-03:事故当事的 `_load_wf_deps`(按已发布工作流依赖开机预热)已删,
        # **但这道门必须留** —— 并发加载源不止那一个:后台 preload_residents 与请求
        # 路径的 get_or_load(runner 按需加载)、UI 手动 load、
        # vLLM 重连全都能同时进来,同一张卡两个 adapter.load() 的竞争条件原样存在。
        self._global_load_lock = asyncio.Lock()
        # round3 #2:load_model 自动选卡(spec.gpu is None → get_best_gpu)后,实际落卡
        # index 是局部变量;OOM 时 load_model raise、还没写进 _models → get_or_load 拿不到
        # 真正 OOM 的卡,退成 evict_lru(None) 驱全局 LRU(可能驱了另一张没满的卡,OOM 的卡
        # 仍满 → 重试再 OOM → 永久毒化)。这里记每个 model 上次尝试的落卡,OOM 时据它精确驱逐。
        # model_id → 上次尝试落的卡(整组)。OOM 重试据此精确驱逐这些卡上的 LRU。
        self._last_attempt_gpus: dict[str, list[int]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _lock_for(self, model_id: str) -> asyncio.Lock:
        return self._locks.setdefault(model_id, asyncio.Lock())

    # ------------------------------------------------------------------
    # 放置决策(2026-09-03 审查):**只在这里**
    # ------------------------------------------------------------------

    @staticmethod
    def _adapter_class(spec: ModelSpec):
        """spec.adapter_class 的类对象(导入失败 → None)。"""
        dotted = spec.adapter_class or ""
        module_path, _, class_name = dotted.rpartition(".")
        if not module_path:
            return None
        try:
            return getattr(importlib.import_module(module_path), class_name, None)
        except Exception:  # noqa: BLE001 — 依赖没装的适配器不该在这里炸
            return None

    def _supports_gpu_group(self, spec: ModelSpec) -> bool:
        """这个模型的适配器接不接受 GPU 组(张量并行)。

        不接受的(TTS 等)即便配了 `gpus` 也按单卡处理 —— 否则 manager 按
        两张卡各预留一半、`loaded_gpus` 显示 0+2,而适配器实际单卡跑,卡 2 的预留是
        幻影、UI 在撒谎(审查 #15/#21)。
        """
        cls = self._adapter_class(spec)
        return bool(getattr(cls, "supports_gpu_group", False)) if cls is not None else False

    def _model_size_gb(self, spec: ModelSpec) -> float:
        """模型体积(GB):优先 spec.vram_mb,没有就量权重目录 —— 与适配器同一把尺子。"""
        if spec.vram_mb and spec.vram_mb > 0:
            return spec.vram_mb / 1024
        main = (spec.paths or {}).get("main")
        if not main:
            return 0.0
        from src.config import get_settings  # noqa: PLC0415
        from src.services.inference._placement import estimate_model_size_gb  # noqa: PLC0415

        path = Path(get_settings().LOCAL_MODELS_PATH) / main
        if not path.exists():
            path = Path(main)
        return estimate_model_size_gb(path)

    def _resolve_placement(self, model_id: str, spec: ModelSpec) -> _Placement:
        """决定这个模型落哪几张卡 —— **放置决策的唯一入口**。

        优先级(前两条是**硬约束**,放不下就报错,绝不自动搬到别的卡):
          1. 显式 `gpus`(GPU 组,张量并行)—— 适配器必须 supports_gpu_group,否则降级单卡 + warning;
          2. 显式 `gpu`(单卡);
          3. 没有显式放置 → 自动:
             a. 配了 `tensor_parallel_size > 1` → 在 hardware.yaml 声明过的同型号组里挑一个;
             b. 单卡装不下 → 同上挑组;挑不到就退单卡(并说清原因);
             c. `vram_mb > 0` → allocator 按实时空闲挑一张(带在途预留);
             d. 其余(外部 vLLM,体积未知)→ 加载后探测。

        适配器拿到的是这里的结论(device + gpus),自己不再选卡:此前适配器会在
        "放不下"时自作主张换到别的卡对,而 manager 记的还是原来那张 —— 预算按 A 卡算、
        CVD 钉 B 卡,启动即 OOM 且日志误导(审查 #13/#22/#24)。
        """
        from src.gpu.topology import resolve_gpus, select_tp_group  # noqa: PLC0415

        explicit = resolve_gpus(spec)
        supports_group = self._supports_gpu_group(spec)

        if len(explicit) > 1 and not supports_group:
            logger.warning(
                "模型 %r 配了 GPU 组 %s,但适配器 %s 不支持组(supports_gpu_group=False)"
                " —— 按单卡 cuda:%d 处理,不做组预留。",
                model_id, explicit, spec.adapter_class, explicit[0],
            )
            explicit = explicit[:1]

        if explicit:
            self._assert_explicit_fits(model_id, spec, explicit, supports_group)
            reserved_group = None
            if len(explicit) > 1 and spec.vram_mb > 0:
                # 组内每张卡各登记「均分后的一份」在途预留 —— 否则并发的 auto 选卡
                # 看不到这组卡即将被占,会撞上来(C1 只覆盖了单卡路径)。
                per_card = max(1, int(spec.vram_mb / len(explicit)))
                self._allocator.reserve_gpus(explicit, per_card)
                reserved_group = (explicit, per_card)
            return _Placement(gpu_indices=explicit, reserved_group=reserved_group)

        # ---- 无显式放置:自动 ----
        try:
            want_tp = int((spec.params or {}).get("tensor_parallel_size") or 0)
        except (TypeError, ValueError):
            want_tp = 0
        size_gb = self._model_size_gb(spec)

        if supports_group and (want_tp > 1 or self._needs_more_than_one_card(size_gb)):
            group = select_tp_group(size_gb, exact_size=want_tp if want_tp > 1 else None)
            if group:
                per_card = max(1, int(size_gb * 1024 / len(group)))
                self._allocator.reserve_gpus(group, per_card)
                logger.info("模型 %r(%.1fG)自动选组 %s 做张量并行 tp=%d",
                            model_id, size_gb, group, len(group))
                return _Placement(gpu_indices=group, reserved_group=(group, per_card))
            logger.error(
                "模型 %r(%.1fG)需要多卡(tensor_parallel_size=%s / 单卡装不下),但 "
                "hardware.yaml 里没有「声明过 + 同型号 + 都装得下分片」的组可用 → 退回单卡。"
                "如确实想跨卡,请在 configs/hardware.yaml 声明该组并给模型配 gpus:[...]",
                model_id, size_gb, want_tp or "-",
            )

        if spec.vram_mb > 0:
            gpu_index = self._allocator.get_best_gpu(spec.vram_mb)
            # C1:选中卡上已原子登记 spec.vram_mb 的在途预留,必须在 load 完成/失败后释放。
            reserved_single = (gpu_index, spec.vram_mb) if gpu_index >= 0 else (-1, 0)
            return _Placement(
                gpu_indices=[gpu_index] if gpu_index >= 0 else [],
                reserved_single=reserved_single,
            )

        # External service (e.g. vLLM) — detect GPUs AFTER the process has actually
        # claimed them (pre-load nvidia-smi returns nothing).
        return _Placement(detect_after_load=True)

    def _needs_more_than_one_card(self, size_gb: float) -> bool:
        """没有任何一张卡装得下(按实时空闲 × 0.85 的余量算)。体积未知 → False。"""
        if size_gb <= 0:
            return False
        try:
            from src.services.gpu_monitor import poll_gpu_stats  # noqa: PLC0415

            stats = poll_gpu_stats()
        except Exception:  # noqa: BLE001
            return False
        if not stats:
            return False
        return all(float(s.get("free_mb") or 0) / 1024 * 0.85 < size_gb for s in stats)

    def _assert_explicit_fits(
        self, model_id: str, spec: ModelSpec, cards: list[int], supports_group: bool
    ) -> None:
        """显式钉卡/钉组是**硬约束**:装不下就 raise,绝不自动搬到别的卡(审查 #24)。

        只对子进程型 LLM 适配器(supports_gpu_group)判定。
        """
        if not supports_group:
            return
        size_gb = self._model_size_gb(spec)
        if size_gb <= 0:
            return
        from src.gpu.topology import group_budget_gb, select_tp_group  # noqa: PLC0415

        _total, free = group_budget_gb(cards)
        if free <= 0:  # 查不到实时显存 → 不拦(别因为 nvidia-smi 抽风就拒绝加载)
            return
        if free * len(cards) >= size_gb:
            return
        suggestion = ""
        alt = select_tp_group(size_gb)
        if alt and alt != cards:
            suggestion = f";可用的同型号组 {alt} 能装下,请改 GPU 分配"
        raise ModelLoadError(
            model_id,
            f"显式分配的 GPU {cards} 装不下该模型(约 {size_gb:.1f}G,组内每卡可用 "
            f"{free:.1f}G × {len(cards)} 张){suggestion}。显式钉卡是硬约束,不会自动换卡。",
        )

    def _instantiate_adapter(
        self, spec: ModelSpec, gpu_group: list[int] | None = None
    ) -> InferenceAdapter:
        """Dynamically import and instantiate adapter from spec.adapter_class dotted path.

        v2: passes `paths: dict[str, str]` to the adapter __init__. Adapters
        (vLLM/SGLang/TTS) read `paths['main']`.
        """
        dotted = spec.adapter_class
        module_path, _, class_name = dotted.rpartition(".")
        if not module_path:
            raise ImportError(
                f"adapter_class '{dotted}' must be a fully-qualified dotted path"
            )
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)

        params = dict(spec.params)

        # 启动参数的运行时覆盖(spec 2026-09-22)。registry 的 _load 直读 yaml、不套 params
        # overlay,且 ModelSpec 是**启动时**建的 frozen 快照 —— 在那儿合并的话 PATCH 之后
        # 就算 unload + load 用的还是旧快照,必须重启后端,与端点「unload + load 生效」的
        # 承诺对不上。所以在这里、**每次 load 时**合并,读的是 DB 的当前值。
        # (2026-09-22 之前这一步整个不存在:端点写了库、GET 报「已生效」,适配器收到的
        #  还是 yaml 原值 —— 特性对 models.d 定义的模型全程是 no-op。)
        #
        # 只认白名单,且与写端点**共用同一个对象**(绝不复制一份,两份迟早分叉):
        # 放置与显存预算是**放置结论**,绝不能从 params 这条路渗回来(那正是 2026-09-11
        # 「改落卡忘改 util」事故的形状)。
        from src.config import (  # noqa: PLC0415
            LAUNCH_PARAM_WHITELIST,
            drop_prefix_caching_vllm_alias,
            load_runtime_overrides,
        )
        ov = load_runtime_overrides().get(spec.id) or {}
        ov_params = {
            k: v for k, v in (ov.get("params") or {}).items()
            if k in LAUNCH_PARAM_WHITELIST
        }
        params.update(ov_params)
        if "enable_prefix_caching" in ov_params:
            # vllm_args 里的同义写法会在 merge_vllm_args 里盖住适配器 kwarg —— 不摘掉的话
            # 这个开关是单向的(开得了、关不掉,且不报错)。摘的是新 dict,见该函数注释。
            params = drop_prefix_caching_vllm_alias(params)

        # 每模型显存预算 overlay(runtime_overrides.json,spec 2026-06-13)。registry 直读
        # models.yaml 不过 overlay,这里按需注入。只给接受该 kwarg 的适配器(vLLM 类),
        # TTS 等不接受的不传以免 unexpected-kwarg。
        import inspect

        vb = ov.get("vram_budget")
        if (
            isinstance(vb, dict)
            and "vram_budget" not in params
            and "vram_budget" in inspect.signature(cls.__init__).parameters
        ):
            params["vram_budget"] = vb

        # GPU 组(张量并行)→ 适配器。落卡不能只靠 load(device)(那只带主卡),
        # vLLM/SGLang 要拿全组来钉 CUDA_VISIBLE_DEVICES + --tensor-parallel-size。
        # 组优先用 manager 这次真正决定的那组(gpu_group),其次 spec 上的显式配置;
        # 只给 supports_gpu_group 的适配器传(其它适配器不接受,传了会 unexpected-kwarg,
        # 而且给它们分组本身就是幻影预留 —— 审查 #15/#21)。
        from src.gpu.topology import resolve_gpus  # noqa: PLC0415
        _group = list(gpu_group or []) or resolve_gpus(spec)
        if (
            len(_group) > 1
            and getattr(cls, "supports_gpu_group", False)
            and "gpus" not in params
            and "gpus" in inspect.signature(cls.__init__).parameters
        ):
            params["gpus"] = list(_group)

        return cls(paths=spec.paths, **params)

    def _detect_vllm_gpus_for_adapter(self, adapter) -> list[int]:
        """Map the adapter's subprocess (and its children) to GPU indices
        via nvidia-smi. Runs AFTER load() — before that, vLLM hasn't
        allocated any devices yet."""
        try:
            # Collect pids belonging to this adapter: main process + descendants.
            root_pid = None
            proc = getattr(adapter, "_process", None)
            if proc is not None:
                root_pid = proc.pid
            adopted = getattr(adapter, "_adopted_pid", None)
            if adopted:
                root_pid = adopted
            if not root_pid:
                return []

            import subprocess
            # Descendants (children + grandchildren); tp>1 spawns worker procs.
            try:
                out = subprocess.run(
                    ["ps", "-o", "pid", "--no-headers", "--ppid", str(root_pid)],
                    capture_output=True, text=True, timeout=3,
                ).stdout
                pids: set[int] = {root_pid}
                for line in out.splitlines():
                    s = line.strip()
                    if s.isdigit():
                        pids.add(int(s))
                # One more hop (tp uses spawn → grandchildren)
                for child in list(pids):
                    out2 = subprocess.run(
                        ["ps", "-o", "pid", "--no-headers", "--ppid", str(child)],
                        capture_output=True, text=True, timeout=3,
                    ).stdout
                    for line in out2.splitlines():
                        s = line.strip()
                        if s.isdigit():
                            pids.add(int(s))
            except Exception:
                pids = {root_pid}

            result = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,gpu_uuid", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return []
            gpu_result = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            uuid_to_idx: dict[str, int] = {}
            for line in gpu_result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    uuid_to_idx[parts[1]] = int(parts[0])

            hits: set[int] = set()
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2:
                    continue
                try:
                    pid_int = int(parts[0])
                except ValueError:
                    continue
                if pid_int in pids:
                    idx = uuid_to_idx.get(parts[1])
                    if idx is not None:
                        hits.add(idx)
            return sorted(hits)
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_loaded(self, model_id: str) -> bool:
        entry = self._models.get(model_id)
        return entry is not None and entry.adapter.is_loaded

    def get_adapter(self, model_id: str) -> InferenceAdapter | None:
        entry = self._models.get(model_id)
        if entry is None:
            return None
        entry.touch()
        return entry.adapter

    async def get_loaded_adapter(self, model_id: str) -> InferenceAdapter:
        """Get adapter, loading on demand if needed.

        v2 unified path for all node-layer adapter calls. Replaces the
        4-line "get_adapter → check is_loaded → load_model → get_adapter"
        pattern duplicated across nodes/llm.py + nodes/audio.py.

        Raises:
            ModelNotFoundError: model_id has no spec (yaml + scan miss).
                                Maps to HTTP 404.
            ModelLoadError:     load failed (recorded in `_load_failures`).
                                Maps to HTTP 503.
        """
        # Fast path: already loaded
        adapter = self.get_adapter(model_id)
        if adapter is not None and adapter.is_loaded:
            # 已加载的健康模型不该留 stale failure(H2:并发另一个尝试失败写的)。
            self._load_failures.pop(model_id, None)
            return adapter

        # Check for prior failure (set by background preload or a previous
        # load_model attempt). Don't retry indefinitely — admin must
        # restart or call load_model explicitly to clear.
        # H2:fast-path 检查后、这里之前有竞争窗口 —— 并发另一个 load 可能刚成功,
        # 而某个失败的并发尝试写了 stale failure。stale failure 不该挡住已加载的健康
        # 模型(虚假 503)。再确认一次 is_loaded:已加载则清 stale 并返回。
        if model_id in self._load_failures:
            adapter = self.get_adapter(model_id)
            if adapter is not None and adapter.is_loaded:
                self._load_failures.pop(model_id, None)
                return adapter
            raise ModelLoadError(model_id, self._load_failures[model_id])

        # Lazy load. load_model raises ValueError("Unknown model") on
        # spec miss; convert to typed ModelNotFoundError.
        try:
            await self.load_model(model_id)
        except ValueError as e:
            if "Unknown model" in str(e):
                raise ModelNotFoundError(model_id) from e
            # Other ValueErrors (e.g. adapter-without-config) are load failures
            self._load_failures[model_id] = str(e)
            raise ModelLoadError(model_id, str(e)) from e
        except Exception as e:
            self._load_failures[model_id] = f"{type(e).__name__}: {e}"
            raise ModelLoadError(model_id, str(e)) from e

        adapter = self.get_adapter(model_id)
        if adapter is None or not adapter.is_loaded:
            self._load_failures[model_id] = "load_model returned but adapter is not loaded"
            raise ModelLoadError(model_id, self._load_failures[model_id])
        return adapter

    @staticmethod
    def _is_oom(exc: BaseException) -> bool:
        """判定异常是不是 CUDA OOM。

        不能在模块顶层 import torch（runner venv 测试里 torch 是 MagicMock，
        且 ModelManager 应能在无 torch 的纯逻辑测试中跑）。改用类名 + 文本判定：
        torch.cuda.OutOfMemoryError 的类名就是 'OutOfMemoryError'；其它库的
        OOM 一般文案里有 'out of memory'。
        """
        name = type(exc).__name__
        if "OutOfMemoryError" in name:
            return True
        return "out of memory" in str(exc).lower()

    async def get_or_load(
        self,
        model_id: str,
        adapter_factory: Callable[[ModelSpec], InferenceAdapter] | None = None,
    ) -> InferenceAdapter:
        """Get adapter, loading on demand with OOM-evict-retry (spec §4.3).

        在 `get_loaded_adapter` 的 lazy-load 之上加一层 OOM 韧性：首次 load 撞
        CUDA OOM → evict 同 GPU 的 LRU 非 resident / 非 referenced 模型 → 重试
        一次。重试仍 OOM（或非 OOM 异常）→ 落 `_load_failures` 并 raise
        ModelLoadError。**runner 子进程的 node-executor 唯一的加载入口** —— OOM
        重试逻辑全收在这里，调用方不感知。

        Raises:
            ModelNotFoundError: model_id 无 spec (HTTP 404).
            ModelLoadError:     load 失败 / 二次 OOM (HTTP 503).
        """
        # Fast path: already loaded
        adapter = self.get_adapter(model_id)
        if adapter is not None and adapter.is_loaded:
            # 已加载的健康模型不该留 stale failure(H2:并发另一个尝试失败写的)。
            self._load_failures.pop(model_id, None)
            return adapter
        # Prior failure check —— 不重试。H2:stale failure 不挡已加载的健康模型
        # (并发另一个 load 刚成功;虚假 503)。再确认一次 is_loaded。
        if model_id in self._load_failures:
            adapter = self.get_adapter(model_id)
            if adapter is not None and adapter.is_loaded:
                self._load_failures.pop(model_id, None)
                return adapter
            raise ModelLoadError(model_id, self._load_failures[model_id])

        spec = self._registry.get(model_id)
        if spec is None:
            spec = self._registry.add_from_scan(model_id)
        if spec is None:
            raise ModelNotFoundError(model_id)

        last_err: BaseException | None = None
        for attempt in range(2):
            try:
                await self.load_model(model_id, adapter_factory=adapter_factory)
                loaded = self.get_adapter(model_id)
                if loaded is None or not loaded.is_loaded:
                    self._load_failures[model_id] = (
                        "load_model returned but adapter is not loaded"
                    )
                    raise ModelLoadError(model_id, self._load_failures[model_id])
                self._load_failures.pop(model_id, None)
                return loaded
            except ModelLoadError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                if self._is_oom(e) and attempt == 0:
                    # 显式钉卡/钉组用 resolve_gpus(spec);自动分配用 load_model 记下的
                    # 实际落卡(round3 #2)—— 否则 evict_lru(None) 驱全局、放跑了真正
                    # OOM 的卡。TP 模型占多张卡,**逐张**驱逐(只驱主卡会漏掉副卡)。
                    from src.gpu.topology import resolve_gpus  # noqa: PLC0415
                    cards = resolve_gpus(spec) or self._last_attempt_gpus.get(model_id) or [None]
                    evicted = [await self.evict_lru(gpu_index=c) for c in cards]
                    logger.warning(
                        "get_or_load(%r): OOM on first load, evicted %r on gpus %s, retrying",
                        model_id, [e for e in evicted if e], cards,
                    )
                    continue
                msg = (
                    f"OOM after evict: {e}" if self._is_oom(e)
                    else f"{type(e).__name__}: {e}"
                )
                self._load_failures[model_id] = msg
                raise ModelLoadError(model_id, msg) from e
        # 防御：循环必定 return 或 raise
        self._load_failures[model_id] = f"{type(last_err).__name__}: {last_err}"
        raise ModelLoadError(model_id, self._load_failures[model_id])

    def get_references(self, model_id: str) -> set[str]:
        return set(self._references.get(model_id, set()))

    def is_in_use(self, model_id: str) -> bool:
        """正在推理中(卸载会 segfault,force 也不覆盖)。供 unload 路由判定 409 的真实原因。

        2026-09-05 审查:没有这个访问器时,路由只能拿 `get_references` 反推原因 ——
        既被引用又正在 infer 时会误报 engine_referenced + 建议 force,调用方照做仍是
        409,死循环。`unload_model` 内部先查 in_use 再查 refs,路由必须同序。
        """
        return model_id in self._in_use

    def add_reference(self, model_id: str, ref_id: str) -> None:
        self._references.setdefault(model_id, set()).add(ref_id)

    def remove_reference(self, model_id: str, ref_id: str) -> None:
        refs = self._references.get(model_id)
        if refs:
            refs.discard(ref_id)

    async def load_model(
        self,
        model_id: str,
        adapter_factory: Callable[[ModelSpec], InferenceAdapter] | None = None,
    ) -> None:
        """Load *model_id* onto the best available GPU.

        Parameters
        ----------
        adapter_factory:
            Optional callable ``(ModelSpec) -> InferenceAdapter``.  When
            omitted the adapter is instantiated via dynamic import from
            ``spec.adapter_class``.
        """
        spec = self._registry.get(model_id)
        if spec is None:
            # Fallback: model wasn't in models.yaml at startup but the
            # disk scanner discovered it later (auto-detected LLM / VL).
            # Synthesize a ModelSpec on the fly so newly-dropped LLM
            # checkpoints don't require a yaml edit + restart.
            spec = self._registry.add_from_scan(model_id)
        if spec is None:
            raise ValueError(f"Unknown model: {model_id!r}")

        async with self._lock_for(model_id):
            if self.is_loaded(model_id):
                self._models[model_id].touch()
                return

            # 放置决策的**唯一入口**(2026-09-03 审查 #13/#24):显式 gpus/gpu 是硬约束,
            # 没有显式放置才自动选组/选卡。适配器只执行这里的结论,不自行换卡。
            pl = self._resolve_placement(model_id, spec)
            gpu_indices, gpu_index = list(pl.gpu_indices), pl.gpu_index
            detect_after_load = pl.detect_after_load
            reserved_gpu, reserved_mb = pl.reserved_single
            reserved_group = pl.reserved_group

            device = f"cuda:{gpu_index}" if gpu_index >= 0 else "cpu"
            # round3 #2:记录本次实际落卡,供 get_or_load 在 OOM 时精确驱逐这些卡
            # (detect_after_load 的外部服务 gpu_index 是占位 0,记了也无害)。
            # 记**整组**:TP 模型 OOM 可能发生在任一张卡上,只记主卡会漏掉副卡。
            if gpu_indices:
                self._last_attempt_gpus[model_id] = list(gpu_indices)
            elif gpu_index >= 0:
                self._last_attempt_gpus[model_id] = [gpu_index]

            # Build adapter + load:统一 try/finally 保证在途预留一定释放。
            # 审查发现的真 bug:adapter 构建在 load 锁之前,若它抛异常,原来放在
            # 锁内 finally 的释放会被跳过 → _pending[gpu] 幻影预留泄漏,那张卡永远
            # 少 spec.vram_mb 可用。释放挪到覆盖「构建 + load」的外层 finally,并删掉
            # 锁内那份避免双重释放(会把 _pending 扣穿)。
            try:
                if adapter_factory is not None:
                    adapter = adapter_factory(spec)
                else:
                    adapter = self._instantiate_adapter(
                        spec, gpu_group=gpu_indices if len(gpu_indices) > 1 else None)

                # 全局串行门:一次只加载一个模型。选卡已由 allocator 在途预留保证并发
                # 安全,这里串行化真正的 adapter.load(),避免多个 vLLM 同一瞬间在同卡
                # (或跨卡)做 CUDA init → engine core init 竞争 / 相关联功率尖峰
                # (2026-07-06 生产事故:35B + embedding 同时上 PRO6000,embedding 起不来)。
                async with self._global_load_lock:
                    await adapter.load(device)
            finally:
                # 权重已落卡(或构建/load 失败)→ 释放在途预留;nvidia-smi 之后能反映真实占用。
                if reserved_gpu >= 0:
                    self._allocator.release_reservation(reserved_gpu, reserved_mb)
                if reserved_group is not None:
                    self._allocator.release_gpus(*reserved_group)

            # 落卡回填(审查 #10):适配器把最终钉进 CUDA_VISIBLE_DEVICES 的卡记在
            # `adapter.gpu_indices`。它是"真正占了哪些卡"的第一手来源 —— 比 nvidia-smi
            # 探测便宜,也比 manager 的预期更准(适配器可能按 tp 收窄了组)。
            adapter_cards = [int(i) for i in (getattr(adapter, "gpu_indices", None) or [])]
            if adapter_cards and adapter_cards != gpu_indices:
                logger.info("模型 %r 实际落卡 %s(manager 预期 %s)—— 以适配器为准",
                            model_id, adapter_cards, gpu_indices or "自动")
                gpu_indices = adapter_cards
                gpu_index = adapter_cards[0]
                self._last_attempt_gpus[model_id] = list(adapter_cards)
            elif detect_after_load:
                # 适配器没报(外部实例/重连)→ 退回 nvidia-smi 探测。
                # _detect_vllm_gpus_for_adapter 内含 subprocess(同步阻塞)→ 丢线程池,
                # 别在 load 后卡事件循环(性能二轮 P2-C)。
                gpu_indices = await asyncio.to_thread(
                    self._detect_vllm_gpus_for_adapter, adapter
                ) or [0]
                gpu_index = gpu_indices[0] if gpu_indices else 0

            self._models[model_id] = LoadedModel(
                spec=spec,
                adapter=adapter,
                gpu_index=gpu_index,
                gpu_indices=gpu_indices,
            )
            self._references.setdefault(model_id, set())
            # Clear prior failure record on successful load; lets admin
            # retry by re-calling load_model after fixing the underlying issue.
            self._load_failures.pop(model_id, None)
            logger.info("Loaded model %r on %s", model_id, device)

    def _model_id_for_adapter(self, adapter: InferenceAdapter) -> str | None:
        """given adapter instance → its model_id(_models 里反查,n 很小)。"""
        for mid, entry in self._models.items():
            if entry.adapter is adapter:
                return mid
        return None

    def mark_adapter_in_use(self, adapter: InferenceAdapter) -> None:
        """node-executor 在 adapter.infer 前调:标记其 model_id 正在用,unload/evict 跳过。
        引用计数 +1(支持同一 adapter 并发 infer)。"""
        mid = self._model_id_for_adapter(adapter)
        if mid is not None:
            self._in_use[mid] = self._in_use.get(mid, 0) + 1

    def release_adapter(self, adapter: InferenceAdapter) -> None:
        """infer 收尾(成功/异常)调:引用计数 -1,减到 0 才清标记。"""
        mid = self._model_id_for_adapter(adapter)
        if mid is not None:
            n = self._in_use.get(mid, 0) - 1
            if n <= 0:
                self._in_use.pop(mid, None)
            else:
                self._in_use[mid] = n

    async def unload_model(self, model_id: str, force: bool = False) -> bool:
        """Unload *model_id*.

        If the model is *resident* or has active references it will NOT be
        unloaded unless *force=True*.
        """
        async with self._lock_for(model_id):
            entry = self._models.get(model_id)
            if entry is None:
                return False

            # in-use 是硬守卫,**强于 force**:绝不卸载正在 infer 的 adapter —— denoise 在
            # to_thread 工作线程跑,卸载会释放它正在用的 CUDA 权重 → segfault。
            if model_id in self._in_use:
                logger.warning(
                    "Skipping unload of in-use model %r(正在 infer,卸载会 segfault)", model_id)
                return False

            # 2026-09-05:这两条拒绝以前是 debug,生产 log level 下等于静默 —— 排查
            # 「unload 报成功但显存不退」时 journal 里一个字都没有。拒绝是运维要看见的
            # 结论(路由现在也据此回 409),升到 info。
            if not force:
                if entry.spec.resident:
                    logger.info("Skipping unload of resident model %r", model_id)
                    return False
                refs = self._references.get(model_id, set())
                if refs:
                    logger.info(
                        "Skipping unload of referenced model %r (refs=%s)",
                        model_id,
                        refs,
                    )
                    return False

            # unload 同步杀 vLLM 子进程(SIGTERM→wait(10s)→SIGKILL→wait(5s),最长 ~15s)。
            # 直接在事件循环上跑会冻结所有 API/SSE/WS。in-use 硬守卫上面已在锁内查过 →
            # to_thread 安全(不会卸载正在 infer 的 adapter)。round-review C2。
            await asyncio.to_thread(entry.adapter.unload)
            del self._models[model_id]
            logger.info("Unloaded model %r", model_id)
            return True

    def set_model_resident(self, model_id: str, resident: bool) -> bool:
        """切模型的常驻位 —— resident=True → evict_lru/unload(非 force)
        跳过它(不被自动驱逐)。

        ModelSpec 是 frozen,经 model_copy 换新 spec(LoadedModel.spec 可重赋)。
        同时同步 registry spec(见 ModelRegistry.set_resident)—— 未加载模型标常驻
        也要让 /health 统计和 preload 名单立即生效,不等重启。registry 没有该 id
        且未加载 → False。
        """
        reg_updated = False
        set_res = getattr(self._registry, "set_resident", None)
        if callable(set_res):
            reg_updated = bool(set_res(model_id, resident))
        entry = self._models.get(model_id)
        if entry is not None:
            entry.spec = entry.spec.model_copy(update={"resident": resident})
        if not reg_updated and entry is None:
            return False
        logger.info("set_model_resident %r → %s", model_id, resident)
        return True

    @property
    def loaded_model_ids(self) -> list[str]:
        return [mid for mid, entry in self._models.items() if entry.adapter.is_loaded]

    def get_pid_map(self) -> dict[int, str]:
        """Return {pid: model_id} for all managed processes that have a PID."""
        result: dict[int, str] = {}
        for mid, entry in self._models.items():
            pid = getattr(entry.adapter, "pid", None)
            if pid is not None:
                result[pid] = mid
        return result

    def get_status(self) -> dict:
        """Return current model manager status."""
        return {
            "loaded": self.loaded_model_ids,
            "references": {k: list(v) for k, v in self._references.items() if v},
            "last_used": {
                mid: entry.last_used for mid, entry in self._models.items()
            },
        }

    def get_model_dependencies(self, workflow: dict) -> list[dict]:
        """Extract model dependencies from workflow nodes."""
        deps: list[dict] = []
        seen: set[str] = set()
        for node in workflow.get("nodes", []):
            node_type = node.get("type", "")
            data = node.get("data", {})
            model_key: str | None = None
            if node_type == "tts_engine":
                model_key = data.get("engine")
            elif node_type == "llm":
                model_key = data.get("model_key")
            if model_key and model_key not in seen:
                spec = self._registry.get(model_key)
                if spec is not None:
                    seen.add(model_key)
                    deps.append({"key": model_key, "type": spec.model_type})
        return deps

    async def preload_residents(
        self,
        on_loaded: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Startup preload of resident models, ordered by `preload_order` (spec 4.2).

        遍历 registry 里所有 `resident:true` 的 spec，按 `preload_order` 升序
        load。`preload_order` 为 None 的排在最后（保持 registry 的 FIFO 顺序）。

        **Fail-soft（spec 4.3）**：单个模型 `load_model` 抛任何异常 → 把原因写进
        `_load_failures[model_id]` + 继续下一个模型，**绝不向上抛**。这样某个
        resident 模型 OOM / 文件损坏不会阻断 API server 启动，也不会阻断后面
        其它 resident 模型的 preload。失败的模型在 `/health` 的 `load_failures`
        里可见，Dashboard 据此显示 degraded banner + Retry。

        Parameters
        ----------
        on_loaded:
            可选回调，每个模型成功 load 后以 `model_id` 调用一次。`main.py` 用它
            做「invalidate engines/models cache + 推 ws/models 事件」。回调本身
            抛异常会被吞掉（best-effort，不影响 preload 流程）。

        LLM 类模型也在此处 preload:LLMRunner 从不自己 spawn vLLM(只做 crash
        recovery/health),旧版这里又排除 llm → resident LLM 没有任何路径随启动
        加载,重启后 /health 的 resident 计数永远缺员、启动横幅卡死。vLLM 的
        实际拉起就是 load_model → VLLMAdapter.load,与手动加载同一条路。
        """
        residents = [s for s in self._registry.specs if s.resident]
        # 升序 key：preload_order 有值的在前（按值升序），None 的统一排到最后。
        # (0, order) < (1, 0) 保证所有 None 都在所有有值的之后。
        residents.sort(
            key=lambda s: (1, 0) if s.preload_order is None else (0, s.preload_order)
        )
        if residents:
            logger.info(
                "Preloading %d resident model(s) in order: %s",
                len(residents), [s.id for s in residents],
            )
        for spec in residents:
            try:
                await self.load_model(spec.id)
            except Exception as e:  # noqa: BLE001 — fail-soft is the whole point
                detail = f"{type(e).__name__}: {e}"
                self._load_failures[spec.id] = detail
                logger.warning("Resident preload failed for %s: %s", spec.id, detail)
                continue
            logger.info("Resident preload succeeded: %s", spec.id)
            if on_loaded is not None:
                try:
                    await on_loaded(spec.id)
                except Exception:  # noqa: BLE001 — callback is best-effort
                    logger.exception("preload_residents on_loaded callback failed for %s", spec.id)

    def _live_policy(self, model_id: str, entry) -> tuple[bool, int]:
        """该模型**当前生效**的 (resident, ttl_seconds) —— 回收类守卫的唯一判据。

        为什么不能直接读 `entry.spec`(2026-09-16 真机事故):`load_model` 对已加载模型
        是早返回(`if self.is_loaded(...): touch(); return`),**不刷新 entry.spec**。
        所以改了 yaml 的 resident/ttl 再 `POST /engines/reload`,换掉的只是
        `registry.specs`,live entry 仍停在首次加载那份旧 spec。结果:
        `wemm_embedding_4b` 明明配了 `resident: true`、UI 也显示 True
        (API 读的是 cfg,见 routes/engines.py `resident=cfg.get(...)`),
        84 分钟后仍被 `TTL expired` 卸掉 —— 守卫读的是旧 spec 的 resident=False。

        registry 查不到就退回 entry.spec:测试注入的 adapter_factory 模型等
        本就不在 registry 里,那些场景 entry.spec 才是唯一来源。
        """
        spec = None
        reg = getattr(self, "_registry", None)
        if reg is not None:
            try:
                spec = reg.get(model_id)
            except Exception:  # noqa: BLE001 —— registry 异常不该让守卫瘫痪
                spec = None
        if spec is None:
            spec = entry.spec
        return bool(getattr(spec, "resident", False)), int(getattr(spec, "ttl_seconds", 0) or 0)

    async def check_idle_models(self) -> None:
        """Unload models that have been idle too long with no references."""
        now = time.monotonic()
        to_unload: list[str] = []
        for mid, entry in list(self._models.items()):
            resident, ttl = self._live_policy(mid, entry)
            if resident:
                continue
            if self._references.get(mid):
                continue
            if ttl <= 0:
                continue
            if now - entry.last_used > ttl:
                to_unload.append(mid)
        for mid in to_unload:
            logger.info("TTL expired: unloading %s", mid)
            await self.unload_model(mid)

    async def stash_model(self, model_id: str) -> bool:
        """整模型 adapter RAM stash(spec 2026-06-12 PR-2):权重挪 CPU,entry 保留,
        命中时 restore 秒回。不可 stash(in_use/resident/被引用/引擎不支持/RAM 水位不足)
        → False,调用方走旧销毁。"""
        async with self._lock_for(model_id):
            entry = self._models.get(model_id)
            if entry is None:
                return False
            if entry.stashed:
                # 已 stash 再收卸载请求 = 用户要真清(RAM 也还回去)→ False 走销毁。
                return False
            if model_id in self._in_use or entry.spec.resident or self._references.get(model_id):
                return False
            try:
                import psutil  # noqa: PLC0415
                need = int(getattr(entry.spec, "vram_mb", 0) or 0) * 1024 * 1024
                if psutil.virtual_memory().available - need < self._stash_ram_reserve_bytes():
                    logger.info("adapter stash 跳过(RAM 水位不足)id=%s", model_id)
                    return False
                ok = await asyncio.to_thread(entry.adapter.stash)
            except Exception as e:  # noqa: BLE001 — stash 失败不挡调用方销毁路径
                logger.warning("adapter stash 失败 id=%s:%s", model_id, e)
                return False
            if not ok:
                return False
            entry.stashed = True
            logger.info("adapter stash → RAM id=%s(~%dMB,命中秒回)", model_id,
                        int(getattr(entry.spec, "vram_mb", 0) or 0))
            return True

    async def evict_lru(self, gpu_index: int | None = None) -> str | None:
        """Evict the least-recently-used non-resident, non-referenced model.

        Parameters
        ----------
        gpu_index:
            When provided, only consider models loaded on that GPU.

        Returns
        -------
        The evicted model_id, or ``None`` if nothing was evicted.
        """
        candidates = [
            entry
            for mid, entry in self._models.items()
            # 与 check_idle_models 同口径:读**当前生效**的 resident,不是 entry 里
            # 那份可能陈旧的 spec(见 _live_policy 的说明)。
            if not self._live_policy(mid, entry)[0]
            and not self._references.get(mid)
            and mid not in self._in_use  # 不驱逐正在 infer 的(否则 segfault)
            # stashed 的不占卡 —— 选它销毁腾不出显存,守卫会误以为腾了 → 重试仍 OOM 空转。
            # 它的 RAM 回收出口 = 手动二次卸载 / stash 水位拒绝(spec 2026-06-12 PR-3)。
            and not getattr(entry, "stashed", False)
            # 张量并行模型占多张卡,某副卡 OOM 时也该能选中它驱逐 —— 早先只比主卡
            # entry.gpu_index,TP 模型的副卡 OOM 永远选不中它(审查发现)。cards() 是
            # 「这个模型占哪些卡」的唯一实现。
            and (gpu_index is None or gpu_index in entry.cards())
        ]

        if not candidates:
            return None

        lru = min(candidates, key=lambda e: e.last_used)
        model_id = lru.spec.id
        # RAM stash(spec 2026-06-12 PR-3):守卫驱逐优先 stash(挪 RAM 待命,命中秒回;
        # 显存同样立即腾出)。不可 stash(引擎不支持/水位/已 stashed)→ 旧销毁。
        if await self.stash_model(model_id):
            logger.info("Evicted(stash) LRU model %r from gpu %s", model_id, lru.gpu_index)
            return model_id
        if await self.unload_model(model_id, force=True):
            logger.info("Evicted LRU model %r from gpu %s", model_id, lru.gpu_index)
            return model_id
        # unload 被 in-use 硬守卫跳过 —— 选候选(排除 in_use)后、unload 前有并发
        # mark_adapter_in_use 插入。显存没真腾出,别谎报 evicted,否则调用方
        # (OOM-evict-retry)以为腾了空转重试。返回 None = 本轮没驱逐成功。
        logger.info("evict_lru: %r 变 in-use,跳过卸载,未腾出显存", model_id)
        return None

    def loaded_models_snapshot(self) -> list[dict]:
        """当前已加载模型的结构化快照,用于跨进程上报(runner 子进程 → 主进程,
        经 Pong)。tts adapter 真加载在 runner 自己的 ModelManager._models 里,
        主进程的 _models 看不到 —— runner 每次 Pong 带上这份快照,主进程聚合后供
        `GET /api/v1/engines/loaded-adapters`、系统状态「已加载模型」读(单一真相来源)。

        每条字段都为「过进程边界 + 还原 UI」够用:model_id、model_type、gpu、vram、
        pipeline_class / source_files(spec.params 里有才非空)、last_used_ago_sec。
        """
        now = time.monotonic()
        out: list[dict] = []
        for mid, entry in self._models.items():
            params = entry.spec.params or {}
            out.append({
                "model_id": mid,
                "model_type": entry.spec.model_type,
                "gpu_index": entry.gpu_index,
                "gpu_indices": list(entry.gpu_indices),
                "vram_mb": entry.spec.vram_mb,
                "pipeline_class": params.get("pipeline_class"),
                "stashed": entry.stashed,  # RAM 待命(不占显存,命中秒回;spec 2026-06-12)
                "source_files": list(params.get("source_files") or []),
                "last_used_ago_sec": round(now - entry.last_used, 2),
            })
        return out

    def stash_ram_bytes(self) -> int:
        """本进程 RAM stash 池占用字节(spec ram-pinned-linkage PR-1b,/monitor/stats 用):
        adapter 级 stash(整模型挪 CPU,按 `entry.spec.vram_mb` 估,无精确字节)。
        best-effort,异常 → 0。"""
        total = 0
        try:
            for entry in self._models.values():
                if getattr(entry, "stashed", False):
                    total += int(getattr(entry.spec, "vram_mb", 0) or 0) * 1024 * 1024
        except Exception:  # noqa: BLE001
            return 0
        return total

    @staticmethod
    def _stash_ram_reserve_bytes() -> int:
        import os  # noqa: PLC0415
        return int(float(os.getenv("NOUS_STASH_RAM_RESERVE_GB", "24")) * 1e9)
