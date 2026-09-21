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


def _basename(file_path) -> str:
    """日志友好的文件名末段(只用于 log,容错 None/空)。"""
    return Path(file_path).name if file_path else str(file_path)


def _modular_repo_from_components(resolved: dict) -> str:
    """从细粒度图组件推 HF-layout repo(含 model_index.json 的目录)。

    ModularPipeline.from_pretrained 要 repo(提供 config/scheduler + clip/vae)。依次从
    unet/clip/vae 组件文件向上找 model_index.json —— **unet 是 comfy 量化单文件(无 repo)时,
    从 clip/vae(指向 HF text_encoder/vae)推 repo**(PR-2:transformer 由桥接 override)。
    """
    for comp_key in ("diffusion_models", "clip", "vae"):
        spec = resolved.get(comp_key)
        if spec is None:
            continue
        f = Path(spec.file)
        for cand in (f.parent, f.parent.parent, f.parent.parent.parent):
            if (cand / "model_index.json").exists():
                return str(cand)
    # 全单文件无 HF repo → 用「架构参考整模型」(单文件装配 PR-2):借其 config/scheduler/tokenizer,
    # 三组件由桥接 override(build_bridged_*)灌单文件权重。架构取 unet 的 adapter_arch。
    unet = resolved.get("diffusion_models")
    arch = getattr(unet, "adapter_arch", None) or "flux2"
    ref = _reference_repo_for_arch(arch)
    if ref:
        return ref
    raise ValueError(
        f"modular 引擎需 HF-layout repo;组件均为单文件且找不到架构 {arch!r} 的参考整模型"
        f"(在 media/diffusers/ 放一个对应整模型作 config 参考)。"
    )


def _is_standalone_single_file(spec) -> bool:
    """任意组件:文件是 repo 外单文件(parent 无 config.json)→ 需桥接 override。
    HF-layout 组件(diffusers/<m>/{transformer,text_encoder,vae}/)parent 有 config.json。"""
    return not (Path(spec.file).parent / "config.json").exists()


def _reference_repo_for_arch(arch: str) -> str | None:
    """架构 → 配置目录(单文件装配:借它的 tokenizer + 各组件 config)。

    **PR-B 起优先返回仓内 bundle**(`backend/configs/image_arch/<arch>/`,几 MB)——
    用户库可不再放参考整模型(18GB)即能跑同架构单文件。Fallback 扫 LOCAL_MODELS_PATH/media/diffusers/
    保留向后兼容(老用户仍能用)。

    支持架构:flux2(已 bundle)。新增 arch 经 PR-C 的 ImageArchSpec 注册表 + 在 configs/ 加 bundle 一并接入。
    """
    import json  # noqa: PLC0415
    from src.config import get_settings  # noqa: PLC0415

    arch_lower = (arch or "").lower()
    if not arch_lower:
        return None

    # 1. 仓内 bundled config(首选,几 MB,完全 self-contained)
    # model_manager.py 在 backend/src/services/ → parents[2] = backend/。
    backend_root = Path(__file__).resolve().parents[2]
    bundled = backend_root / "configs" / "image_arch" / arch_lower
    if (bundled / "transformer" / "config.json").is_file():
        return str(bundled)

    # Fallback: scan complete Diffusers models for a matching architecture.
    base = Path(get_settings().LOCAL_MODELS_PATH) / "media" / "diffusers"
    if not base.is_dir():
        return None
    # hint 子串匹配 model_index._class_name(小写)。z-image 的 _class_name=ZImagePipeline(无连字符)
    # → 映射成 "zimage" 才命中(PR-2,spec 2026-06-09 分开载入借参考库 config)。
    hint = {"flux2": "flux2", "flux1": "flux", "ernie": "ernie", "z-image": "zimage",
            "ideogram4": "ideogram"}.get(arch_lower, arch_lower)
    if not hint:
        return None
    for d in sorted(base.iterdir()):
        mi = d / "model_index.json"
        if not mi.is_file():
            continue
        try:
            cls = str(json.loads(mi.read_text()).get("_class_name", "")).lower()
        except Exception:  # noqa: BLE001
            continue
        if hint in cls:
            return str(d)
    return None

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
    # 命中 restore 秒回)。组件路线 combo 不用此位(组件层 stash 在 L1 池)。
    stashed: bool = False

    # InferenceAdapter is a non-pydantic ABC instance; allow as field value.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def cards(self) -> list[int]:
        """这个模型占着哪几张卡 —— 「gpu_indices 优先、退回主卡」这条回退逻辑的唯一实现
        (此前散在 evict_lru / _evictable_mb_on_card / _resolve_auto_card 三处)。"""
        idxs = [int(i) for i in (self.gpu_indices or []) if i is not None and i >= 0]
        if idxs:
            return idxs
        return [self.gpu_index] if self.gpu_index is not None and self.gpu_index >= 0 else []


# PR-1 Task 6: components are lighter than full LoadedModel — they're just a loaded
# state_dict/module dict + metadata. Stored in ModelManager._components.
LoadedComponent = dict  # opaque to ModelManager: {_state_dict, spec, loaded_at, ...}


class ModelManager:
    """Unified model lifecycle manager: load, unload, evict, reference-count."""

    def __init__(self, registry: ModelRegistry, allocator: GPUAllocator) -> None:
        self._registry = registry
        self._allocator = allocator
        self._models: dict[str, LoadedModel] = {}
        self._references: dict[str, set[str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # device=auto 粘性放置(#199):device-independent combo identity → 上次落的 gpu_index。
        # 同一工作流二次跑 auto 解析回同一张卡,避免 combo_key 翻卡 → cache miss → 重载。
        self._image_stick: dict[tuple, int] = {}
        # 反向:model_id → stick identity。unload/evict 时据此清 _image_stick,避免 stale
        # stick 指向已卸载/已满的卡 + 绕过 VRAM 守卫 → 反复 OOM(#210 回归修复)。
        self._image_stick_keys: dict[str, tuple] = {}
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
        # 路径的 get_or_load(runner 按需加载)、UI 手动 load、组件 preload 端点、
        # vLLM 重连全都能同时进来,同一张卡两个 adapter.load() 的竞争条件原样存在。
        self._global_load_lock = asyncio.Lock()
        # round3 #2:load_model 自动选卡(spec.gpu is None → get_best_gpu)后,实际落卡
        # index 是局部变量;OOM 时 load_model raise、还没写进 _models → get_or_load 拿不到
        # 真正 OOM 的卡,退成 evict_lru(None) 驱全局 LRU(可能驱了另一张没满的卡,OOM 的卡
        # 仍满 → 重试再 OOM → 永久毒化)。这里记每个 model 上次尝试的落卡,OOM 时据它精确驱逐。
        # model_id → 上次尝试落的卡(整组)。OOM 重试据此精确驱逐这些卡上的 LRU。
        self._last_attempt_gpus: dict[str, list[int]] = {}
        # 组件级 L1 缓存(spec 2026-06-02):同一组件(file|load_device|dtype|loras = 一个 id)
        # 被多个 combo 共享时只加载一份、跨 combo 复用。key = L1 component key(见
        # `_l1_component_key`,用真实 load_device 而非 spec.device);value = LoadedComponent dict
        # {module, role, key, refs:set[combo_id], resident:bool, last_used, device}。
        # **只对 offload=none 组件建/复用**(带 cpu/cuda offload hook 的模块共享会冲突,spec §3);
        # image runner 单串行队列 → 无并发加载 → 无需加锁(spec §0)。
        self._components: dict = {}
        # Forward-ref the ComponentKey type as a tuple alias so the module-top
        # import stays clean (component_spec doesn't import ModelManager).
        from src.services.inference.component_spec import ComponentKey  # noqa: F401
        # PR-D4(2026-05-28):删 `_image_adapters` / `_image_adapter_locks` 双套
        # 路径。image adapter 走 derived model_id 入 `_models` 统一字典,LRU
        # 驱逐 / 引用计数 / 状态可见 / pid_map 全部 ModelManager 底层自动覆盖,
        # 不再单独管 image。derive 规则见 `_derive_image_model_id`。

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

        不接受的(image/tts/SeedVR2…)即便配了 `gpus` 也按单卡处理 —— 否则 manager 按
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
            if gpu_index < 0 and spec.model_type == "image":
                # Image adapters use diffusers `enable_model_cpu_offload` — weights live
                # in CPU RAM and only the active block enters GPU during inference.
                # "no GPU has 24GB free" doesn't mean we can't run; it means we share.
                # Fall back to GPU 0 so /api/v1/engines doesn't surface "running · GPU -1".
                logger.warning(
                    "Image model %r: allocator found no GPU with %dMB free; "
                    "falling back to GPU 0 (cpu_offload mode shares it).",
                    model_id, spec.vram_mb,
                )
                return _Placement(gpu_indices=[0])
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

        只对子进程型 LLM 适配器(supports_gpu_group)判定 —— image 走 cpu_offload,
        "装不下"不等于跑不了,那条路保持原样。
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

        v2: passes `paths: dict[str, str]` to the adapter __init__. Single-component
        adapters (vLLM/SGLang/TTS) read `paths['main']`; image-class adapters
        read `paths['transformer']`, `paths['text_encoder']`, `paths['vae']`.

        For image specs, lora_paths is auto-injected from the lora_scanner
        unless the yaml entry already supplied one. This means yaml never
        has to enumerate individual LoRAs — drop a .safetensors into a
        configured LORA_PATHS dir and it's available next adapter load.
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
        if spec.model_type == "image" and "lora_paths" not in params:
            # Inject ALL scanned LoRAs (no arch filter). Pre-existing
            # workflows that reference an "incompatible" LoRA name should
            # still be loadable here so the adapter can produce its own
            # friendly error at apply-time (image_diffusers.py:243 catches
            # "zero matching weights" and explains the architecture
            # mismatch). Filtering at injection-time silently breaks those
            # workflows with a confusing "not in registered lora_paths"
            # message — see post-mortem in PR #75 successor.
            #
            # The chip count on /api/v1/engines stays arch-aware (via
            # `count_loras_for_arches(accepts)` in engines.py) so the UI
            # still tells the truth about how many LoRAs are likely usable.
            from src.services.lora_scanner import get_lora_paths
            params["lora_paths"] = get_lora_paths()
        # accepts_lora_archs is yaml-only metadata, never passed to adapter
        params.pop("accepts_lora_archs", None)

        # 每模型显存预算 overlay(runtime_overrides.json,spec 2026-06-13)。registry 直读
        # models.yaml 不过 overlay,这里按需注入。只给接受该 kwarg 的适配器(vLLM 类),
        # image 等不接受的不传以免 unexpected-kwarg。
        import inspect

        from src.config import load_runtime_overrides
        ov = load_runtime_overrides().get(spec.id) or {}
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
            # 组件 L1 refcount 释放(spec §2):此 combo 引用的组件 refs 减;refs 空 + 非 resident
            # 才真出池。共享组件被别的 combo 用着则保留(不误伤,否则卸了在用的 → segfault)。
            # adapter.unload 已 empty_cache,但那时组件还被 _components 持着没真释放 —— 出池后
            # 断了最后强引用,再清一次缓存让显存真降。
            if self._release_combo_components(model_id):
                try:
                    import gc  # noqa: PLC0415
                    import torch  # noqa: PLC0415
                    gc.collect()
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001 — 清缓存 best-effort
                    pass
            # 清 device=auto 粘性(#210 回归):adapter 没了,stick 不能再指向它的卡并跳 VRAM
            # 守卫 —— 否则下次同工作流粘回已卸载/可能已满的卡反复 OOM。
            sk = self._image_stick_keys.pop(model_id, None)
            if sk is not None:
                self._image_stick.pop(sk, None)
            logger.info("Unloaded model %r", model_id)
            return True

    def set_model_resident(self, model_id: str, resident: bool) -> bool:
        """切已加载 by-key 模型(如 SeedVR2)的常驻位 —— resident=True → evict_lru/unload(非 force)
        跳过它(不被自动驱逐)。组件 L1 PR-2c:引擎库 SeedVR2 卡常驻 toggle。

        ModelSpec 是 frozen,经 model_copy 换新 spec(LoadedModel.spec 可重赋)。
        同时同步 registry spec(见 ModelRegistry.set_resident)—— 未加载模型标常驻
        也要让 /health 统计和 preload 名单立即生效,不等重启。registry 没有该 id
        (by-key 模型如 SeedVR2 combo)且未加载 → False。
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
            # 组件级 L1 共享池(spec §1/§2):每个组件 role/文件/卡 + 哪些 combo 在引用它
            # (refs)+ 是否常驻。跨 combo 复用的组件 refs 会有多个 combo_id —— 真机 smoke
            # 验「A、B 共用 X-bf16/vaeZ」就看这里 refs 是否含两个 combo。
            "components": [
                {
                    "role": c["role"],
                    "file": _basename(c["key"][0]),
                    "device": c["key"][1],
                    "dtype": c["key"][2],
                    "state_key": c.get("state_key"),
                    "refs": sorted(c["refs"]),
                    "resident": c["resident"],
                }
                for c in self._components.values()
            ],
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

        registry 查不到就退回 entry.spec:测试注入的 adapter_factory 模型、L1 组件等
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
        命中时 restore 秒回。不可 stash(in_use/resident/被引用/引擎不支持/RAM 水位不足/
        L1 池组件 combo)→ False,调用方走旧销毁。"""
        async with self._lock_for(model_id):
            entry = self._models.get(model_id)
            if entry is None:
                return False
            if entry.stashed:
                # 已 stash 再收卸载请求 = 用户要真清(RAM 也还回去)→ False 走销毁。
                return False
            if model_id in self._in_use or entry.spec.resident or self._references.get(model_id):
                return False
            # 组件路线 combo:其组件在 L1 池有 refs,整 pipe .to(cpu) 会把池组件一起挪走
            # 而池记账不知情 → 不在 adapter 层 stash(组件层 PR-1 已覆盖)。
            if any(model_id in c["refs"] for c in self._components.values()):
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
        # 显存同样立即腾出)。不可 stash(L1-combo/offload/水位/已 stashed)→ 旧销毁。
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

    # --- PR-1 Task 6: component-level cache APIs -----------------------------
    #
    # Per spec §5.5 these coexist with the legacy load_model/is_loaded/unload_model.
    # PR-2's ImageSampler will exercise these directly without going through yaml.

    # PR-4 收官:legacy 组件 L1 缓存(_component_lock_for / is_component_loaded /
    # get_or_load_component / _load_component_impl / _load_component_module /
    # unload_component / _base_spec)已删 —— 仅服务自写 DiffusersImageBackend 引擎,
    # 已随 image_diffusers/image_sampler 删除。modular 引擎走 ComponentsManager +
    # build_bridged_transformer(image_modular),不用此缓存。

    _VRAM_EST_MB = {"diffusion_models": 18000, "clip": 6000, "vae": 1000}

    @staticmethod
    def _component_bytes(file_path: str) -> int:
        """组件权重字节数;**分片整模型**(...-NNNNN-of-NNNNN.safetensors)时 file 只是第 1 片,
        返回同组件所有 sibling 分片之和。统一显存估算的分片感知入口 —— 选卡 need / 可驱逐 vram_mb /
        守卫 per-card need 全走它,口径一致(2026-06-08 真机:vram_mb 只算第 1 片 → 可驱逐空间低估 →
        effective free 不足 → auto 退 CPU hang)。"""
        import glob
        import os
        import re
        sz = os.path.getsize(file_path)
        if re.search(r"-\d+-of-\d+\.safetensors$", os.path.basename(file_path)):
            pattern = re.sub(r"-\d+-of-\d+\.safetensors$", "-*-of-*.safetensors", file_path)
            shards = glob.glob(pattern)
            if shards:
                sz = sum(os.path.getsize(s) for s in shards)
        return sz

    def _component_need_mb(self, spec) -> int:
        """估该组件载入需多少显存(MB):优先真实文件大小(分片求和)×1.3,拿不到回退 _VRAM_EST_MB 表。
        (anima 2B ~10GB ≠ Flux2 9B 18GB,套固定表会误判。)

        分片整模型用 _component_bytes 求和所有分片;否则 6GB 片 ×1.3 严重低估 38GB transformer →
        auto 挑 24GB 小卡 → 加载到一半 OOM(2026-06-08 Qwen-Image-Edit 角度控制真机逮到)。"""
        need = self._VRAM_EST_MB.get(spec.kind, 8000)
        try:
            sz_mb = int(self._component_bytes(spec.file) / (1024 * 1024) * 1.3)
            if sz_mb > 0:
                need = sz_mb
        except (OSError, AttributeError, TypeError):
            pass
        return need

    @staticmethod
    def _repo_total_mb(transformer_file: str) -> int:
        """HF-layout 整模型 repo 总权重(MB):file=<repo>/transformer/x.safetensors 上溯两级,
        有 model_index.json 才算 repo(否则 0 = 单文件细粒度,走旧逻辑)。
        为什么需要:checkpoint 三件套(transformer/clip/vae)之外 repo 还有组件 ——
        Ideogram-4 的 unconditional_transformer(18.6G)不在三件套,按三件之和估 footprint
        低估 ~25G,auto 误判「装得下」贴边挤卡(2026-06-12 PR-3 验收真机逮到)。"""
        from pathlib import Path  # noqa: PLC0415
        try:
            repo = Path(transformer_file).parent.parent
            if not (repo / "model_index.json").exists():
                return 0
            total = sum(f.stat().st_size for f in repo.rglob("*.safetensors"))
            return int(total / (1024 * 1024))
        except OSError:
            return 0

    def _colocated_auto_footprint_mb(self, components: dict) -> int:
        """transformer(lead)device=auto 选卡用的**整模型同卡 footprint**(MB):会跟 transformer
        同卡常驻的所有组件 need 之和 = device=auto 且 offload=none 的组件(auto 组件下游被强制跟
        transformer 卡;offload!=none 的组件 forward 时才上卡、不常驻 → 不计)。

        修 2026-06-08 真机 OOM 根因:旧逻辑对每个组件**各自**按 `_component_need_mb`(单件)选卡 →
        transformer 单件估值小、让 24G 小卡看着够 → get_best_gpu 选小卡 → clip/vae 强制跟卡 →
        整模型(transformer+clip+vae)压小卡 OOM(Flux2 派 3090)。按整模型 footprint 选卡后,
        auto 会路由到「真空闲+可驱逐」装得下整模型的大卡(如驱逐 Z-Image 残留后的 Pro6000)。"""
        # checkpoint 整模型(HF-layout repo):按 repo 总权重一把估(含 unconditional_transformer
        # 等三件套之外的组件)。三件套都是 auto+none(整模型同卡常驻)才适用;否则回退逐件。
        dm = components.get("diffusion_models")
        if dm is not None and all(
                (c := components.get(k)) is not None and c.device == "auto"
                and (getattr(c, "offload", "none") or "none") == "none"
                for k in ("diffusion_models", "clip", "vae")):
            repo_mb = self._repo_total_mb(dm.file)
            if repo_mb:
                return int(repo_mb * 1.1)
        total = 0
        for k in ("diffusion_models", "clip", "vae"):
            s = components.get(k)
            if s is None or s.device != "auto":
                continue
            if (getattr(s, "offload", "none") or "none") != "none":
                continue
            total += self._component_need_mb(s)
        # Ideogram-4 单文件第二 DiT(unconditional)不在三件套 —— 与 cond DiT 同卡常驻,补进 footprint
        # (整模型路走 _repo_total_mb 已含;单文件路 repo_mb=0 落到这里,漏了它会低估 ~9-18G 误派小卡 OOM)。
        if dm is not None and dm.device == "auto" and (getattr(dm, "offload", "none") or "none") == "none":
            _uf = getattr(dm, "unconditional_file", None)
            if _uf:
                try:
                    total += int(self._component_bytes(_uf) / (1024 * 1024) * 1.3)
                except (OSError, AttributeError, TypeError):
                    pass
        return total

    # 流式分块工作集(MB):单块上卡 + stream 预取 + 激活。spike 2026-06-12:Ideogram-4
    # 峰值 22.8G - TE 实占 ~16.3G - vae ≈ ~6G,但 _component_need_mb 对驻卡件已 ×1.3 过保守,
    # 这里取 5G 平衡(过保守会把 24G 卡误判装不下,降级路径永远不触发)。
    _STREAM_WORKSET_MB = 5120

    def _stream_footprint_mb(self, components: dict) -> int:
        """offload=stream 模式的同卡 footprint:驻卡组件(clip/vae,offload=none)之和
        + 流式工作集。transformer 权重驻 RAM 轮转,不计。"""
        total = self._STREAM_WORKSET_MB
        for k in ("clip", "vae"):
            comp = components.get(k)
            if comp is None or (getattr(comp, "offload", "none") or "none") != "none":
                continue
            total += self._component_need_mb(comp)
        return total

    def _stream_pin_estimate_mb(self, dm_spec) -> int:
        """流式分块预 pin 的 RAM 占用估计(MB):被流式的 = DiT 权重(transformer +
        Ideogram-4 第二 DiT)。整模型 repo 路按 repo 总权重估(偏保守:含 TE/VAE,
        但它们加载期本也在 RAM,保守=更早降级,err-safe);单文件路 = DiT 文件 + uncond 文件。
        spec ram-pinned-linkage PR-2:门禁据此判 host RAM/pinned 预算装不装得下。"""
        repo_mb = self._repo_total_mb(dm_spec.file)
        if repo_mb:
            return repo_mb
        try:
            total = self._component_bytes(dm_spec.file)
            uf = getattr(dm_spec, "unconditional_file", None)
            if uf:
                total += self._component_bytes(uf)
            return int(total / (1024 * 1024))
        except (OSError, AttributeError, TypeError):
            return self._component_need_mb(dm_spec)

    def _should_stream_low_ram(self, dm_spec) -> bool:
        """流式挂载前的 RAM 门禁 + 降级判定(spec ram-pinned-linkage PR-2)。

        预 pin(DiT ~37G,pin_memory 拷贝)不可换页,叠加 stash 池可能压爆 host RAM/swap。
        梯子:① 先腾 stash 池给预 pin 腾地方;② 腾完仍不足水位 **或** 超 pinned 预算 →
        返回 True = 降级 low_cpu_mem_usage(逐块临时锁页,pinned 有界,挂载快但每步重 pin)。
        估算失败 → False(保持全量预 pin 最快路径,零回归)。永不拒绝。"""
        try:
            import psutil  # noqa: PLC0415
            from src.services.inference.pinned_stash import (  # noqa: PLC0415
                pin_budget_bytes, total_pinned_bytes,
            )
            need = self._stream_pin_estimate_mb(dm_spec) * 1024 * 1024
            reserve = self._stash_ram_reserve_bytes()
            avail = psutil.virtual_memory().available
            if avail - need < reserve:
                self._trim_stash_lru(extra_need=need)  # ① 先腾 stash 池
                avail = psutil.virtual_memory().available
            if avail - need < reserve:  # ② 腾完仍不足
                logger.info(
                    "流式分块:腾 stash 后 host RAM 仍紧(可用 ~%dMB,预 pin 需 ~%dMB,水位 ~%dMB)"
                    "→ 降级逐块临时锁页(挂载快/每步重 pin)",
                    avail // (1024 * 1024), need // (1024 * 1024), reserve // (1024 * 1024))
                return True
            if total_pinned_bytes() + need > pin_budget_bytes():  # ② 超 pinned 预算
                logger.info(
                    "流式分块:预 pin ~%dMB + 已 pin ~%dMB 超预算 ~%dMB → 降级逐块临时锁页",
                    need // (1024 * 1024), total_pinned_bytes() // (1024 * 1024),
                    pin_budget_bytes() // (1024 * 1024))
                return True
            return False
        except Exception:  # noqa: BLE001 — 门禁 best-effort,失败保持全量预 pin
            return False

    def _evictable_mb_on_card(self, idx: int) -> int:
        """该卡上「可驱逐」image adapter 的估计显存之和(非常驻/未被引用/未在 infer)——
        = 守卫「先腾后载」能从这张卡腾出来的量。已加载在用的 combo 自身也算可驱逐(若没在
        infer),所以 auto 粘性命中(combo 已驻该卡)时该卡 effective free 仍判为「装得下」。"""
        total = 0
        for mid, e in self._models.items():
            # 张量并行模型跨多张卡,只比主卡会把它在副卡上的那份显存当成"腾不出来"
            # → 副卡被低估、守卫误判装不下。按全组算,且每张卡只计**均分后的一份**
            # (整份计到每张卡上是重复计数)。
            cards = e.cards()
            if idx not in cards:
                continue
            if e.spec.resident or self._references.get(mid) or mid in self._in_use:
                continue
            if getattr(e, "stashed", False):
                continue  # RAM stash:权重已挪 CPU,该卡 free 已含这部分,计入=双计
            total += max(0, int((getattr(e.spec, "vram_mb", 0) or 0) / max(1, len(cards))))
        return total

    def _card_effective_free_mb(self, idx: int) -> int | None:
        """该卡「真空闲 + 可驱逐空间」(None=查不到 free)。守卫先腾后载后实际能用的量。"""
        free = self._free_vram_mb(f"cuda:{idx}")
        if free is None:
            return None
        return free + self._evictable_mb_on_card(idx)

    def _resolve_auto_card(self, need_mb: int) -> int:
        """**只增强 auto**(spec 2026-06-07):先按「真空闲」挑(allocator,守组隔离);没卡有
        真空闲装得下,再按「真空闲 + 可驱逐空间」挑(守卫会先腾后载);都不行返回 -1(退 CPU)。
        显式选卡不走这(尊重用户选的卡)。"""
        # reserve=False:这是放置探测(可能对多个组件反复调),不做在途占坑,否则会泄漏预留。
        # 图像路径经 runner 单消费者串行,无并发撞卡问题(C1 的占坑只用于主进程 load_model)。
        idx = self._allocator.get_best_gpu(need_mb, reserve=False)
        if idx >= 0:
            return idx
        # 没卡有真空闲装得下 → 看哪张卡「腾掉空闲 adapter 后」装得下,挑 free+evictable 最大的。
        best, best_eff = -1, -1
        # 张量并行模型占多张卡 → 候选卡集合要并上整组,否则副卡永远进不了候选。
        _cards = {c for e in self._models.values() for c in e.cards()}
        for i in _cards:
            eff = self._card_effective_free_mb(i)
            if eff is not None and eff >= need_mb and eff > best_eff:
                best, best_eff = i, eff
        return best

    def _resolve_component_device(self, spec):
        """Resolve device='auto' → 'cuda:N'(算上可驱逐空间)。Returns a NEW spec
        (model_copy keeps validators) so the original descriptor is untouched."""
        if spec.device != "auto":
            return spec
        idx = self._resolve_auto_card(self._component_need_mb(spec))
        resolved = f"cuda:{idx}" if idx >= 0 else "cpu"
        return spec.model_copy(update={"device": resolved})

    @staticmethod
    def _free_vram_mb(device: str) -> int | None:
        """目标卡空闲显存(MB)。'cpu'/'auto'/无 GPU/查询失败 → None(跳过保护)。
        用 nvidia-smi(避免 import torch);best-effort,失败不阻塞。"""
        if not device.startswith("cuda:"):
            return None
        try:
            idx = int(device.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
        try:
            import subprocess
            out = subprocess.run(
                ["nvidia-smi", f"--id={idx}",
                 "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode != 0:
                return None
            return int(out.stdout.strip().splitlines()[0])
        except Exception:  # noqa: BLE001 — best-effort,任何失败都跳过保护
            return None

    @staticmethod
    def _estimate_image_vram_mb(resolved: dict) -> int | None:
        """粗估整模型显存需求(MB)= 三组件 resident bytes 之和 * 余量系数。任一文件不存在
        (纯逻辑测试用 stub 路径)→ None(无法估,跳过保护)。

        fp8 weight-only(torchao):transformer + clip 权重 fp8 存储 ≈ bf16 文件的一半,
        所以这两件按 file bytes * 0.5 估(否则按 bf16 文件字节会高估 → 误拦本可装下的 fp8)。
        """
        import os
        total = 0
        for k in ("diffusion_models", "clip", "vae"):
            try:
                # 分片求和(_component_bytes),否则只算第 1 片 → vram_mb 低估 → 可驱逐空间被低估
                # → effective free 不足 → auto 退 CPU(2026-06-08 真机:Z-Image vram_mb 只算 ~14G
                # 而非 40G,Flux2 找不到可驱逐的大卡)。
                sz = ModelManager._component_bytes(resolved[k].file)
            except OSError:
                return None
            # fp8 量化只作用于 transformer/clip(vae 不量化)
            if k in ("diffusion_models", "clip") and (resolved[k].dtype or "").lower().startswith("fp8"):
                sz //= 2
            total += sz
        # LoRA 权重注入 transformer → 真占显存,旧估算漏了(守卫会低估 → 放过实际会 OOM 的装载)。
        # path 有且文件在才加(name-only / 不存在的不计)。
        for lora in (getattr(resolved["diffusion_models"], "loras", None) or []):
            lp = getattr(lora, "path", None)
            if lp:
                try:
                    total += os.path.getsize(lp)
                except OSError:
                    pass
        # 1.3x 余量(激活/中间张量;含 VAE decode 峰值的粗略 headroom);bytes → MB
        return int(total / (1024 * 1024) * 1.3)

    # 守卫腾显存时给 forward 留的余量(MB):腾到刚好 = need 仍可能 activation OOM。
    # 对照 ComfyUI minimum_inference_memory + EXTRA_RESERVED_VRAM(spec 2026-06-07)。
    _GUARD_RESERVE_MB = 1024

    async def _free_image_vram_on_card(self, card: str, need_mb: int) -> int | None:
        """按卡 LRU 驱逐空闲 image adapter,直到该卡空闲 ≥ need_mb 或无可驱逐(对照 ComfyUI
        free_memory,spec 2026-06-07)。复用 evict_lru(gpu_index)(只驱逐 非resident/非引用/
        非in-use 的 adapter;vLLM/LLM 不在 image _models → 天然不被驱逐)。返回最终空闲 MB
        (None=查不到 → 调用方按「不阻塞」处理)。"""
        if not card.startswith("cuda:"):
            return None
        idx = int(card.split(":")[1])
        free = self._free_vram_mb(card)
        while free is not None and free < need_mb:
            evicted = await self.evict_lru(gpu_index=idx)
            if evicted is None:  # 该卡已无可驱逐(只剩 resident / in-use / vLLM)
                break
            logger.info("守卫先腾后载:为装载 evict 了 %s,腾后 %s 空闲≈%sMB",
                        evicted, card, self._free_vram_mb(card))
            free = self._free_vram_mb(card)
        return free

    async def _guard_image_vram_per_card(self, resolved: dict) -> None:
        """逐卡 LLM 卡保护(逐组件选卡 spec 2026-06-04 + 先腾后载 spec 2026-06-07):
        按各组件落的卡分组估需求,**空闲不足时先按卡 LRU 驱逐空闲 image adapter 腾地方
        (对照 ComfyUI free_memory),腾够再放行;腾不出才清晰报错(不静默 OOM)。**

        逐组件跨卡时三组件可能分散到不同卡 —— 旧版只查 transformer 卡会漏查
        clip/vae 落的卡。任一文件不存在(stub 测试)→ 跳过(无法估)。
        """
        import os  # noqa: PLC0415
        need_by_card: dict[str, int] = {}
        for k in ("diffusion_models", "clip", "vae"):
            spec = resolved[k]
            # 逐组件 offload(cpu/cuda:N)的组件不常驻 compute 卡(forward 时才挪上来)→ 不计入该卡预算。
            if (getattr(spec, "offload", "none") or "none") != "none":
                continue
            dev = spec.device
            if not dev.startswith("cuda:"):  # cpu / auto(已解析)等不查
                continue
            # **该组件已在 runner L1 池(本卡常驻)→ combo 装配会复用、不需新显存 → 不计入。**
            # 修用户报告:节点四态显「已加载」(组件确在池里)但 combo 是 cache miss(装配过的 pipe
            # 没缓存),守卫按「全新载入」估满模型尺寸 → 把本可复用的载入误拦成显存不足。
            # 用真实 _components 池判(非主进程 four-state 镜像 —— 镜像可能 stale;池里真有=能复用,
            # 池里没有=确要新载,即便镜像显已加载也得拦,避免静默 OOM)。
            if self._l1_component_key(spec, dev) in self._components:
                continue
            try:
                sz = self._component_bytes(spec.file)  # 分片求和(口径同选卡/vram_mb)
            except OSError:
                continue
            if k in ("diffusion_models", "clip") and (spec.dtype or "").lower().startswith("fp8"):
                sz //= 2
            if k == "diffusion_models":
                for lora in (getattr(spec, "loras", None) or []):
                    lp = getattr(lora, "path", None)
                    if lp:
                        try:
                            sz += os.path.getsize(lp)
                        except OSError:
                            pass
                # Ideogram-4 第二 DiT(unconditional)与 cond DiT 同卡常驻 —— 计进该卡守卫预算
                # (否则少算 ~9-18G,先腾后载会少驱逐 → 装到一半 OOM 而非优雅报错。选卡 footprint
                # 已含,守卫漏算是 2026-06-13 review 逮到的 PR-3 缺口)。fp8 同 cond 减半。
                _uf = getattr(spec, "unconditional_file", None)
                if _uf:
                    try:
                        _usz = self._component_bytes(_uf)
                        if (spec.dtype or "").lower().startswith("fp8"):
                            _usz //= 2
                        sz += _usz
                    except OSError:
                        pass
            need_by_card[dev] = need_by_card.get(dev, 0) + sz
        for dev, need_bytes in need_by_card.items():
            need_mb = int(need_bytes / (1024 * 1024) * 1.3)
            # 先腾后载:空闲不足时按卡 LRU 驱逐空闲 adapter 腾地方(留 reserve 余量),腾完再看。
            free_mb = await self._free_image_vram_on_card(dev, need_mb + self._GUARD_RESERVE_MB)
            if free_mb is not None and free_mb < need_mb:
                raise RuntimeError(
                    f"{dev} 空闲显存不足({free_mb}MB < 约需 {need_mb}MB,已尝试驱逐空闲模型仍不足)—— "
                    f"该卡可能被常驻 LLM / 正在使用的模型占满。换张卡(device)、用更低精度(fp8)、"
                    f"把组件分到别的卡,或启用 offload=cpu(让大模型自动倒换 CPU)。"
                )

    @staticmethod
    def _explain_image_combo_key(combo_key: tuple) -> dict:
        """把 combo_key 拆成 readable dict 用于日志/调试,让 cache miss 时一眼看出
        哪个字段变化导致 derived_id 不同。PR-D5 诊断 cache 命中率用。

        combo_key shape:
          (pipeline_class, offload, comp_offloads, transformer_key, clip_key, vae_key)
        每个 component_key shape:
          (file, device, dtype, frozenset[(lora_name, strength), ...])
        """
        def _explain_comp(ck: tuple) -> dict:
            if not isinstance(ck, tuple) or len(ck) < 4:
                return {"raw": repr(ck)}
            file, device, dtype, loras = ck[0], ck[1], ck[2], ck[3]
            return {
                "file": file,
                "device": device,
                "dtype": dtype,
                "loras": sorted(f"{n}@{s}" for n, s in (loras or set())),
            }
        if not isinstance(combo_key, tuple) or len(combo_key) < 6:
            return {"raw": repr(combo_key)}
        pclass, offload, comp_offloads, t_key, c_key, v_key = combo_key
        return {
            "pipeline_class": pclass,
            "offload": offload,
            "comp_offloads": comp_offloads,
            "transformer": _explain_comp(t_key),
            "clip": _explain_comp(c_key),
            "vae": _explain_comp(v_key),
        }

    @staticmethod
    def _derive_image_model_id(combo_key: tuple) -> str:
        """combo_key = (pipeline_class, offload, transformer_key, clip_key, vae_key)
        → 一个稳定可读的 model_id 串,作为 `_models` 字典的键。

        命名:`image:<pipeline_class>:<transformer_basename>:<short_hash>`
        - pipeline_class 让人看出引擎家族(Flux2Klein / Anima);
        - transformer_basename 是单文件名或 HF repo 末段,辨识具体模型;
        - short_hash 兜底 dtype/offload/LoRA 等剩余差异,**保证 1-1 对应** combo_key。

        derived id 不进 yaml registry — 它只活在 `_models` 运行时字典里,被
        evict_lru / loaded_model_ids / unload_model / get_pid_map 自动消费。
        """
        import hashlib
        pipeline_class, _offload, _comp_offloads, t_key, *_rest = combo_key
        # transformer key shape: (file, device, dtype, frozenset(loras))
        t_file = (t_key[0] if isinstance(t_key, tuple) and t_key else "unknown") or "unknown"
        # 取文件名末段(/path/to/Flux2-Klein-9B-True-v2-bf16.safetensors → Flux2-...-bf16)
        from os.path import basename, splitext
        t_base = splitext(basename(str(t_file)))[0] or "main"
        # 短 hash 区分 dtype/offload/clip/vae/LoRA 等剩余维度
        payload = repr(combo_key).encode("utf-8")
        short_hash = hashlib.sha256(payload).hexdigest()[:8]
        return f"image:{pipeline_class}:{t_base}:{short_hash}"

    def _synthesize_image_spec(
        self,
        model_id: str,
        adapter_class_path: str,
        target_device: str,
        vram_mb: int,
        pipeline_class: str,
        source_files: list[str] | None = None,
    ) -> "ModelSpec":
        """构造 in-flight ModelSpec 给 derived image adapter 用 — 不入 yaml registry,
        只让 LoadedModel.spec 字段有合规对象,这样 evict_lru / loaded_model_ids
        都能正常工作(它们都读 entry.spec.resident / entry.spec.id 等字段)。

        resident=False 让 image adapter 默认可 LRU 驱逐;ttl_seconds 设极大值
        (image adapter 不靠 TTL 释放,靠 LRU + 手动 unload + reference)。

        source_files:组装该 adapter 的源组件文件(unet/clip/vae)。image adapter
        加载在 runner 子进程,主进程靠 `loaded_models_snapshot()` → Pong 上报后,
        要用这些文件把 runner 里的 adapter 映射回引擎库卡片(card 是文件,adapter
        是 combo hash id)。存进 params 随 snapshot 一起过进程边界。
        """
        return ModelSpec(
            id=model_id,
            model_type="image",
            adapter_class=adapter_class_path,
            paths={},  # paths 已被 adapter 内部 build_bridged_* 消化,registry 不再需要
            vram_mb=vram_mb,
            params={"pipeline_class": pipeline_class, "source_files": source_files or []},
            resident=False,
            ttl_seconds=10**9,  # 不靠 TTL 回收(LRU + 手动 unload 才是主路径)
        )

    def loaded_models_snapshot(self) -> list[dict]:
        """当前已加载模型的结构化快照,用于跨进程上报(runner 子进程 → 主进程,
        经 Pong)。image/tts adapter 真加载在 runner 自己的 ModelManager._models 里,
        主进程的 _models 看不到 —— runner 每次 Pong 带上这份快照,主进程聚合后供
        `/image-cache`、系统状态「已加载模型」、引擎库 loaded 视图读(单一真相来源)。

        每条字段都为「过进程边界 + 还原 UI」够用:model_id(combo hash)、model_type、
        gpu、vram、pipeline_class、source_files(映射回引擎卡)、last_used_ago_sec。
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

    def loaded_components_snapshot(self) -> list[dict]:
        """已加载**单组件**(`_components` L1 池)结构化快照,跨进程上报(runner → 主进程经 Pong)。

        引擎库据此标单组件 loaded@卡 + resident —— **含预加载的、不属于任何 combo 的组件**
        (loaded_models_snapshot 只覆盖 combo adapter,够不着 PR-2a 预加载的孤组件)。组件 L1 PR-3a。
        """
        now = time.monotonic()
        out: list[dict] = []
        for comp in self._components.values():
            file_, dev, dtype = comp["key"][0], comp["key"][1], comp["key"][2]  # 第5元 uncond_file 不入快照
            out.append({
                "state_key": comp.get("state_key"),
                "role": comp["role"],
                "file": file_,
                "device": dev,
                "dtype": dtype,
                "resident": comp["resident"],
                "refs_count": len(comp["refs"]),
                "stashed": bool(comp.get("stashed")),  # RAM 待命(不占显存,命中秒回)
                "last_used_ago_sec": round(now - comp["last_used"], 2),
            })
        return out

    def stash_ram_bytes(self) -> int:
        """本进程 RAM stash 池占用字节(spec ram-pinned-linkage PR-1b,/monitor/stats 用):
        组件级 stash(`comp["stash_bytes"]`,已 stash 才有)+ adapter 级 stash(整 pipe 挪 CPU,
        按 `entry.spec.vram_mb` 估,无精确字节)。best-effort,异常 → 0。"""
        total = 0
        try:
            for comp in self._components.values():
                if comp.get("stashed"):
                    total += int(comp.get("stash_bytes") or 0)
            for entry in self._models.values():
                if getattr(entry, "stashed", False):
                    total += int(getattr(entry.spec, "vram_mb", 0) or 0) * 1024 * 1024
        except Exception:  # noqa: BLE001
            return 0
        return total

    async def get_or_load_image_adapter(self, components: dict, pipeline_class: str = "Flux2KleinPipeline", on_event=None, offload: str = "none"):
        """PR-4 entry for the runner component path. Resolves auto devices,
        loads/reuses base modules via the component L1 cache, assembles (or
        reuses) a DiffusersImageBackend keyed by the full 3-component combo.

        OOM resilience: on first-load CUDA OOM, evict legacy LRU then retry once.

        PR-5a: on_event(key, state, error) — 可选异步回调，每个组件触发：
          - "loading": 开始加载 base 模块之前
          - "loaded":  加载成功后（combo cache HIT 时三个组件都触发）
          - "failed":  加载抛异常时（随后 re-raise）
        on_event=None 时行为与 PR-4 完全一致（_emit 是 no-op）。
        """
        from src.services.inference.component_spec import to_component_key, component_state_key

        async def _emit(spec, state, error=None):
            if on_event is not None:
                await on_event(component_state_key(spec), state, error)

        # 整模型同卡 footprint 选卡(修 2026-06-08 真机 OOM 根因,见 _colocated_auto_footprint_mb):
        # transformer(lead)device=auto 时按「会同卡常驻组件之和」选卡,不是 transformer 单件 ——
        # 否则单件估值让小卡看着够、clip/vae 强制跟卡后整模型压小卡 OOM。clip/vae 仍逐件 resolve
        # (下游强制跟 transformer 卡,其 resolve 结果会被覆盖,保留以兼容显式跨卡)。
        _auto_fp = self._colocated_auto_footprint_mb(components)
        resolved = {}
        for k, s in components.items():
            if k == "diffusion_models" and s.device == "auto":
                idx = self._resolve_auto_card(_auto_fp)
                # lowvram PR-2(spec 2026-06-12):整模型无卡可容(含先腾后载)→ 退 CPU 前
                # 按「流式分块」口径(TE/VAE 驻卡 + 流式工作集)再试 —— 装得下则自动降级
                # offload=stream(~6× 慢但从不能跑到能跑)。只在用户没显式选 offload
                # (=none)且非 fp8(stream 不支持在线量化,引擎 fail-loud)时降级。
                if (idx < 0 and (getattr(s, "offload", "none") or "none") == "none"
                        and "fp8" not in (s.dtype or "")):
                    stream_fp = self._stream_footprint_mb(components)
                    s_idx = self._resolve_auto_card(stream_fp)
                    if s_idx >= 0:
                        logger.info(
                            "auto 选卡:整模型 ~%dMB 无卡可容 → 自动启用流式分块落 cuda:%d"
                            "(驻卡口径 ~%dMB,速度约 1/6;权重驻内存逐块上卡)",
                            _auto_fp, s_idx, stream_fp)
                        resolved[k] = s.model_copy(
                            update={"device": f"cuda:{s_idx}", "offload": "stream"})
                        continue
                resolved[k] = s.model_copy(update={"device": f"cuda:{idx}" if idx >= 0 else "cpu"})
            else:
                resolved[k] = self._resolve_component_device(s)
        # device=auto 粘性放置(#199 根治):同一工作流(同 file/dtype/loras)二次跑,把
        # diffusion_models 的 auto 解析粘回上次落的卡 —— 否则 get_best_gpu 按「此刻最空」
        # 会翻卡(run1 占了 A,run2 落 B)→ combo_key 变 → cache miss → 同模型重载(70G 累积)。
        # clip/vae 随 target(下方强制),所以只粘 diffusion_models 即可。
        stick_key = self._image_stick_identity(components, pipeline_class, offload)
        sticky = (components["diffusion_models"].device == "auto"
                  and stick_key in self._image_stick)
        if sticky:
            stuck = self._image_stick[stick_key]
            # **不粘到已满的卡**(只增强 auto,spec 2026-06-07):粘的卡「真空闲+可驱逐」仍装不下
            # → 弃粘,改用上面 _resolve_auto_card 挑的卡(并让守卫在新卡上跑)。combo 已驻该卡时
            # 它自身算可驱逐 → effective free 判得下 → 仍粘(cache hit 不被破坏,触发重载)。
            # 粘卡可行性也按整模型 footprint 判(与上面选卡一致)——否则按单件判「粘卡装得下」、
            # 实际整模型装不下,又回到误派小卡。
            need_dm = _auto_fp
            eff = self._card_effective_free_mb(stuck)
            if eff is None or eff >= need_dm:
                resolved["diffusion_models"] = resolved["diffusion_models"].model_copy(
                    update={"device": f"cuda:{stuck}"})
            else:
                sticky = False
                logger.info("auto 粘性放置弃用:cuda:%s 腾完仍装不下 ~%sMB → 改落 %s",
                            stuck, need_dm, resolved["diffusion_models"].device)
        # 逐组件选卡(spec 2026-06-04):device=auto 让三组件各自 resolve 到不同卡。
        # **auto 跟随 transformer 卡**(整模型单卡的零回归默认),**显式选卡则保留**
        # (逐组件跨卡 —— 把 clip/vae 放与 transformer 不同的卡)。
        target = resolved["diffusion_models"].device
        for k in ("clip", "vae"):
            explicit = (components[k].device or "auto") != "auto"
            if not explicit and resolved[k].device != target:
                resolved[k] = resolved[k].model_copy(update={"device": target})
        # 角色名对齐 pipe 子模块;传给 ModularImageBackend 做逐组件放置 + 逐组件 offload
        #(全同卡且全同 offload → 单卡路径,零回归)。
        comp_devices = {
            "transformer": resolved["diffusion_models"].device,
            "text_encoder": resolved["clip"].device,
            "vae": resolved["vae"].device,
        }
        comp_offloads = {
            "transformer": getattr(resolved["diffusion_models"], "offload", "none") or "none",
            "text_encoder": getattr(resolved["clip"], "offload", "none") or "none",
            "vae": getattr(resolved["vae"], "offload", "none") or "none",
        }
        # Ideogram-4 第二 DiT(unconditional)逐组件放置/offload:跟 cond DiT 同卡同 offload
        # (spec 2026-06-13:_place_components_per_device 据此把它一并挂 cpu-stash 轮转 hook,
        # 双 DiT 一时刻只一个上卡 → 峰值降到 TE+1DiT,spike 验 53G→35G)。
        if getattr(resolved["diffusion_models"], "unconditional_file", None):
            comp_devices["unconditional_transformer"] = resolved["diffusion_models"].device
            comp_offloads["unconditional_transformer"] = comp_offloads["transformer"]
            # **TE(Qwen3-VL)不能 offload**:diffusers pipeline 直调 `text_encoder.language_model.
            # embed_tokens(token_ids)`(绕过 forward hook),offload 后 embed_tokens 权重留 CPU、token 在卡
            # → device 错配。策略 = 只卸 DiT、TE 驻卡(spike 验)。故仅当 TE 被 offload 时 fail-loud。
            if comp_offloads["text_encoder"] != "none":
                raise RuntimeError(
                    "Ideogram-4 的 Qwen3-VL 文本编码器暂不支持 offload(embed_tokens 直调绕过 hook → device "
                    "错配)。请让 Load CLIP 的 offload=none(驻卡);双 DiT(Load Diffusion Model)可 offload=cpu "
                    "塞小卡(TE 驻卡 + 双 DiT 轮转,fp8 下 ~17G 入 24G 卡)。")
        # lowvram 流式 RAM 门禁 + 降级(spec ram-pinned-linkage PR-2):流式预 pin(DiT 权重
        # ~37G,pin_memory 拷贝)不可换页,叠 stash 池可能压爆 host RAM/swap。挂载前先腾 stash、
        # 仍不足或超 pinned 预算则降级 low_cpu_mem_usage(逐块临时锁页,永不拒绝)。auto 降级与
        # 用户显式选 stream 共用此门禁。**不进 combo_key**(同 combo 两种 pin 策略出图相同,进键
        # 白白翻倍缓存)。
        wants_stream = (offload == "stream"
                        or any(v == "stream" for v in comp_offloads.values()))
        stream_low_ram = self._should_stream_low_ram(resolved["diffusion_models"]) if wants_stream else False
        # LLM 卡保护:**逐卡**检查空闲显存(每张卡上**常驻**组件之和 vs 该卡空闲)→ 装载前清晰报错。
        # 守卫内部跳过 offload!=none 的组件(它们 forward 时才挪上卡,不常驻)。
        # combo_key 包含 offload(整管线)+ 逐组件 offload:不同 offload 模式是不同的 pipe 实例
        #(hook 不同,不能跨 offload 复用);逐组件 offload 不进 to_component_key,故单列进 combo_key
        # 避免「同 file/卡、不同 offload」误命中。**上移到守卫前**:据此判断 combo 是否已加载。
        combo_key = (pipeline_class, offload,
                     tuple(comp_offloads[r] for r in ("transformer", "text_encoder", "vae"))) + tuple(
            to_component_key(resolved[k]) for k in ("diffusion_models", "clip", "vae"))
        # LLM 卡保护守卫的跳过条件:**仅 combo 已加载**(精确 cache hit:同 combo_key 已在
        # _models)→ 零新显存,跳守卫安全。
        # 注:旧版还把「sticky」也算跳过条件(假设粘性=cache hit),但 sticky 现在只是「落卡偏好」
        # (可能粘到需 evict 才装得下的卡)→ 不能跳守卫,否则漏了先腾后载会 OOM。已加载用精确的
        # already_loaded 判;sticky 命中真 cache 时 already_loaded 也为 True,照样跳。
        # (best-effort 读 _models,锁外;并发同 combo 装载中漏判最多多跑一次守卫,不会误放真 OOM。)
        already_loaded = self._models.get(self._derive_image_model_id(combo_key)) is not None
        if not already_loaded:
            await self._guard_image_vram_per_card(resolved)
        # 多架构注册表(spec 2026-06-07 P0):按 pipeline_class 反查架构选后端引擎。
        # adapter="anima" → AnimaImageBackend(2B 自定义 DiT);其余(flux2/z-image/qwen-edit…)
        # → ModularImageBackend(标准 diffusers pipeline)。加新架构不用改这里。
        from src.services.inference.model_arch_adapter import arch_spec_by_pipeline  # noqa: PLC0415
        _arch = arch_spec_by_pipeline(pipeline_class)
        if _arch is not None and _arch.adapter == "anima":
            adapter = await self._get_or_load_anima_adapter(
                resolved, combo_key, target, _emit, offload)
        else:
            adapter = await self._get_or_load_modular_adapter(
                resolved, combo_key, pipeline_class, target, _emit, offload, comp_devices, comp_offloads,
                stream_low_ram=stream_low_ram)
        # 记住这工作流落的卡 —— 下次 auto 粘回来(load 与 cache hit 都刷新,target 即当前卡)。
        if target.startswith("cuda:"):
            self._image_stick[stick_key] = int(target.split(":", 1)[1])
            # 反向登记,unload/evict 时据此清 stick(防 stale)。
            self._image_stick_keys[self._derive_image_model_id(combo_key)] = stick_key
        return adapter

    @staticmethod
    def _image_stick_identity(components: dict, pipeline_class: str, offload: str) -> tuple:
        """device-independent combo identity:同一工作流(同 file/dtype/loras/pipeline/offload)
        无论解析到哪张卡都同一 identity。用作 _image_stick 的 key,让 device=auto 二次跑粘回
        同卡 → combo_key 稳定 → cache hit。"""
        parts = []
        for k in ("diffusion_models", "clip", "vae"):
            s = components[k]
            loras = frozenset(
                (lo.name, float(lo.strength)) for lo in (getattr(s, "loras", None) or []))
            parts.append((s.file, s.dtype, loras))
        return (pipeline_class, offload, tuple(parts))

    async def _get_or_load_anima_adapter(self, resolved, combo_key, target, _emit, offload: str):
        """Anima(2B 自定义 DiT)adapter 装配 —— spec 2026-05-26-anima-port,PR-anima-6 集成。

        跟 _get_or_load_modular_adapter 不同:不走桥接 build_bridged_*(那是 Flux2 转换 ComfyUI
        单文件给 diffusers 用的),anima 直接由 arch_anima.AnimaPipeline.from_components 装配
        anima 单文件 + qwen3 + qwen-image VAE。tokenizer 路径走 env NOUS_ANIMA_QWEN_TOKENIZER。
        """
        from src.services.inference.image_anima import AnimaImageBackend  # noqa: PLC0415

        model_id = self._derive_image_model_id(combo_key)
        async with self._lock_for(model_id):
            # cache hit → touch LRU + return
            entry = self._models.get(model_id)
            if entry is not None:
                logger.info("image adapter HIT id=%s (anima)", model_id)
                entry.touch()
                for k in ("diffusion_models", "clip", "vae"):
                    await _emit(resolved[k], "loaded")
                return entry.adapter
            # cache miss — dump combo_key 字段,方便复盘是哪个字段导致不复用
            logger.info(
                "image adapter MISS id=%s (anima) combo=%s",
                model_id, self._explain_image_combo_key(combo_key),
            )

            for k in ("diffusion_models", "clip", "vae"):
                await _emit(resolved[k], "loading")
            try:
                paths = {
                    "transformer": resolved["diffusion_models"].file,
                    "text_encoder": resolved["clip"].file,
                    "vae": resolved["vae"].file,
                }
                adapter = AnimaImageBackend(
                    paths=paths,
                    device=target,
                    dtype=resolved["diffusion_models"].dtype,
                    pipeline_class="AnimaPipeline",
                    offload=offload,
                )
                await adapter.load(target)
                # 预热在首次 infer 时 lazy(_ensure_pipe);不像 Flux2 在装 adapter 时就构建 pipe
                # —— anima pipe 装配 = 4.5s 加载,留首次 infer 一起付,UX 不卡 startup。
            except Exception as e:  # noqa: BLE001
                for k in ("diffusion_models", "clip", "vae"):
                    await _emit(resolved[k], "failed", f"{type(e).__name__}: {e}")
                raise

            for k in ("diffusion_models", "clip", "vae"):
                await _emit(resolved[k], "loaded")
            gpu_idx = int(target.split(":")[1]) if target.startswith("cuda:") else 0
            self._models[model_id] = LoadedModel(
                spec=self._synthesize_image_spec(
                    model_id=model_id,
                    adapter_class_path="src.services.inference.image_anima.AnimaImageBackend",
                    target_device=target,
                    vram_mb=self._estimate_image_vram_mb(resolved) or 0,
                    pipeline_class="AnimaPipeline",
                    source_files=[resolved[k].file for k in ("diffusion_models", "clip", "vae")],
                ),
                adapter=adapter,
                gpu_index=gpu_idx,
                gpu_indices=[gpu_idx],
            )
            return adapter

    async def get_or_load_seedvr2_adapter(
        self,
        model_dir: str,
        dit_model: str | None = None,
        vae_model: str | None = None,
        device: str = "cuda",
        dit_config: dict | None = None,
        vae_config: dict | None = None,
        tensor_offload: str = "cpu",
        enable_debug: bool = False,
    ):
        """SeedVR2 超分 adapter 装配/复用 —— 跟 anima/modular 不同:SeedVR2 不是「三组件
        (diffusion_models/clip/vae)」模型,是「DiT + 专用 VAE 整套自带」的上采样器(by
        model_dir,非 component combo)。所以走**独立 by-key 路径**,不经 component-combo
        机制(get_or_load_image_adapter 那套 device-auto/sticky/三组件强制同卡都不适用)。

        但仍登记进统一 `_models` 字典 → 复用 LRU 驱逐 / loaded_models_snapshot 跨进程上报 /
        手动 unload,跟 Flux2/anima 同一套生命周期管理。

        model_id = `image:SeedVR2:<dit_base>:<short_hash>`(hash 兜底 model_dir/vae/device)。
        """
        import hashlib  # noqa: PLC0415
        from os.path import basename, splitext  # noqa: PLC0415

        from src.services.inference.image_seedvr2 import (  # noqa: PLC0415
            DEFAULT_DIT,
            DEFAULT_VAE,
            SeedVR2UpscaleBackend,
        )

        # 三节点对齐:dit/vae config(device/blockswap/tiling/attention)。model 名优先取 config。
        dcfg = dict(dit_config or {})
        vcfg = dict(vae_config or {})
        dit = dcfg.get("model") or dit_model or DEFAULT_DIT
        vae = vcfg.get("model") or vae_model or DEFAULT_VAE
        # device=auto:SeedVR2 不走 component sticky;直接挑最空的卡(7B 给 Pro 6000)。
        # get_best_gpu 返 -1 = 没卡装得下 → 回退 cuda:0,让下游 OOM 报真错(不静默)。
        # DiT config 显式给 device 则尊重(对齐 ComfyUI 用户选卡);否则用 device 参数 / auto 解析。
        target = dcfg.get("device") or device
        if target in ("auto", "cuda"):
            # reserve=False:放置探测(seedvr2 走 runner 串行路径),不占坑,避免泄漏预留。
            best = self._allocator.get_best_gpu(SeedVR2UpscaleBackend.estimated_vram_mb, reserve=False)
            target = f"cuda:{best}" if best is not None and best >= 0 else "cuda:0"

        dit_base = splitext(basename(str(dit)))[0] or "main"
        # 缓存键纳入 dit/vae config 的 load-time 维度(blockswap/tiling/device/attention/
        # torch_compile)—— 不同配置 = 不同 prepare_runner 实例,不能复用。node_id 不进键
        # (ComfyUI 内部 id 无关)。compile args 是 dict → repr(sorted items) 稳定串化。
        def _compile_key(cfg: dict) -> str | None:
            args = cfg.get("torch_compile_args")
            return repr(sorted(args.items())) if isinstance(args, dict) else None

        key_cfg = {
            "dit_device": dcfg.get("device"), "vae_device": vcfg.get("device"),
            "blocks_to_swap": dcfg.get("blocks_to_swap"), "swap_io": dcfg.get("swap_io_components"),
            "dit_offload": dcfg.get("offload_device"), "vae_offload": vcfg.get("offload_device"),
            "attention": dcfg.get("attention_mode"),
            "enc_tiled": vcfg.get("encode_tiled"), "enc_ts": vcfg.get("encode_tile_size"),
            "enc_to": vcfg.get("encode_tile_overlap"), "dec_tiled": vcfg.get("decode_tiled"),
            "dec_ts": vcfg.get("decode_tile_size"), "dec_to": vcfg.get("decode_tile_overlap"),
            "tensor_offload": tensor_offload,  # 增强阶段 tensor offload(setup_generation_context,load-time)
            "dit_compile": _compile_key(dcfg), "vae_compile": _compile_key(vcfg),
        }
        payload = repr((model_dir, dit, vae, target, sorted(key_cfg.items()))).encode("utf-8")
        short_hash = hashlib.sha256(payload).hexdigest()[:8]
        model_id = f"image:SeedVR2:{dit_base}:{short_hash}"

        async with self._lock_for(model_id):
            entry = self._models.get(model_id)
            if entry is not None:
                logger.info("image adapter HIT id=%s (seedvr2)", model_id)
                entry.touch()
                return entry.adapter
            logger.info("image adapter MISS id=%s (seedvr2) dir=%s dit=%s", model_id, model_dir, dit)

            adapter = SeedVR2UpscaleBackend(
                paths={"model_dir": model_dir, "dit": dit, "vae": vae},
                device=target,
                dit_config=dcfg,
                vae_config=vcfg,
                tensor_offload=tensor_offload,
                enable_debug=enable_debug,
            )
            await adapter.load(target)

            gpu_idx = int(target.split(":")[1]) if target.startswith("cuda:") else 0
            self._models[model_id] = LoadedModel(
                spec=self._synthesize_image_spec(
                    model_id=model_id,
                    adapter_class_path="src.services.inference.image_seedvr2.SeedVR2UpscaleBackend",
                    target_device=target,
                    vram_mb=SeedVR2UpscaleBackend.estimated_vram_mb,
                    pipeline_class="SeedVR2",
                    source_files=[dit, vae],
                ),
                adapter=adapter,
                gpu_index=gpu_idx,
                gpu_indices=[gpu_idx],
            )
            return adapter

    @staticmethod
    def _l1_component_key(spec, load_device: str):
        """组件 L1 缓存 key —— 用**真实 load_device**(非 spec.device,后者可能是 'auto')。

        to_component_key 用 spec.device;但 device='auto' 时两个落到不同卡的 combo 会算出
        同一个 'auto' key → 误复用跨卡模块(card1 的 vae 装到 card2 的 pipe)。这里把 device
        字段换成真实 load_device,保证同卡才命中。file/dtype/loras 仍来自 to_component_key。
        """
        from src.services.inference.component_spec import to_component_key  # noqa: PLC0415
        file_, _dev, dtype, loras, uncond = to_component_key(spec)
        return (file_, load_device, dtype, loras, uncond)

    @staticmethod
    def _state_key_from_l1(key) -> str:
        """L1 key tuple → component_state_key 同款串(file|device|dtype|loras)。

        与 `component_state_key(spec)` 格式严格一致(前端按 loader-node 描述符算同款串来匹配)。
        存进组件 dict 供 resident toggle / 跨进程快照按串匹配,不用反构 ComponentSpec。
        """
        file_, dev, dtype, loras = key[0], key[1], key[2], key[3]  # 第5元 unconditional_file 不进状态串
        lora_sig = "+".join(sorted(f"{name}@{strength}" for name, strength in loras))
        return f"{file_}|{dev}|{dtype}|{lora_sig}"

    def set_component_resident(self, state_key: str, resident: bool) -> bool:
        """按 component_state_key 切已加载组件的常驻位(引擎库 toggle)。匹配上 → 设并返回 True;
        没加载该组件 → False。取消常驻后组件按 refs 走正常 LRU(refs 空即可在显存压力时让出)。"""
        for comp in self._components.values():
            if comp.get("state_key") == state_key:
                comp["resident"] = resident
                logger.info("component L1 set_resident role=%s file=%s → %s",
                            comp["role"], _basename(comp["key"][0]), resident)
                return True
        return False

    def unload_image_component(self, state_key: str) -> bool:
        """按 component_state_key 卸载已预加载组件(引擎库「出缓存」,统一模型管理收尾 PR-1)。
        清常驻;refs 空(无 combo 在用)→ 出 L1 池 + 断 module 强引用 + empty_cache 真释放显存;
        refs 非空(某 combo 正用它)→ 只清常驻,待 combo 释放时随 _release_combo_components 自然出池
        (不硬拔,避免拽掉在用组件致出图崩)。匹配上返回 True,没加载该组件 → False。"""
        hit = False
        freed = False
        for key, comp in list(self._components.items()):
            if comp.get("state_key") != state_key:
                continue
            hit = True
            comp["resident"] = False
            if not comp["refs"]:
                self._components.pop(key, None)
                comp["module"] = None  # 断最后强引用让 gc 回收 CUDA 存储
                freed = True
                logger.info("component L1 引擎库卸载 role=%s file=%s(refs 空,出池释放)",
                            comp.get("role"), _basename(key[0]))
            else:
                logger.info("component L1 引擎库卸载 role=%s file=%s(refs=%s 在用 → 清常驻待自然释放)",
                            comp.get("role"), _basename(key[0]), sorted(comp["refs"]))
        if freed:
            try:
                import torch  # noqa: PLC0415
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001 — best-effort
                pass
        return hit

    async def _get_or_build_image_component(
        self, role, build_fn, spec, repo, load_device, offload, combo_id,
    ):
        """逐组件 L1:命中复用已加载模块,未命中 build + 存池。返回桥接模块。

        - **offload != none → 不进 L1 共享池**(cpu/cuda offload 的 enable_model_cpu_offload
          hook 绑 pipeline,两 combo 共享带 hook 同模块会换入换出冲突 → segfault,spec §3)。
          各 combo 自己 build 一份(等同改动前行为)。
        - offload == none → 干净模块,A、B 引用同一份无害 → 进池共享 + refcount。
        重 build 经 to_thread 不阻塞 runner 事件循环。
        """
        if offload != "none":
            return await asyncio.to_thread(build_fn, spec, repo, load_device)
        key = self._l1_component_key(spec, load_device)
        comp = self._components.get(key)
        if comp is not None:
            if comp.get("stashed"):
                # RAM stash 命中:搬回身份卡(秒级,免磁盘冷载)。失败 → 出池走 MISS 重建。
                try:
                    await asyncio.to_thread(self._restore_component, comp)
                except Exception as e:  # noqa: BLE001
                    logger.warning("component restore 失败,出池重建:%s", e)
                    regs = comp.pop("pin_regs", None)
                    if regs:
                        from src.services.inference.pinned_stash import unpin  # noqa: PLC0415
                        unpin(regs)
                    self._components.pop(key, None)
                    comp = None
        if comp is not None:
            comp["refs"].add(combo_id)
            comp["last_used"] = time.monotonic()
            logger.info(
                "component L1 HIT role=%s file=%s dev=%s refs=%s",
                role, _basename(spec.file), load_device, sorted(comp["refs"]))
            return comp["module"]
        logger.info(
            "component L1 MISS role=%s file=%s dev=%s — building",
            role, _basename(spec.file), load_device)
        module = await asyncio.to_thread(build_fn, spec, repo, load_device)
        self._components[key] = {
            "module": module, "role": role, "key": key,
            "state_key": self._state_key_from_l1(key),
            "refs": {combo_id}, "resident": False,
            "last_used": time.monotonic(), "device": load_device,
        }
        return module

    def _release_combo_components(self, combo_id: str) -> bool:
        """卸 combo 时减它引用的组件 refs;refs 空 + 非 resident → 出池真释放。

        返回是否有组件被释放(调用方据此决定要不要再 empty_cache —— adapter.unload
        已 empty_cache 过,但那时组件还被 _components 持着没真释放;出池后才需再清一次)。
        共享组件被别的 combo 用着(refs 非空)→ 保留,不误伤(spec §2 refcount 正确性)。
        """
        freed = False
        for key, comp in list(self._components.items()):
            if combo_id not in comp["refs"]:
                continue
            comp["refs"].discard(combo_id)
            if not comp["refs"] and not comp["resident"]:
                # RAM stash(spec 2026-06-12):优先挪 CPU 留池待命(下次同键命中秒级搬回,
                # 免磁盘冷载);RAM 水位不够或 .to() 失败 → 旧行为出池销毁。
                if self._stash_component(comp):
                    freed = True  # 权重已离卡,调用方仍需 empty_cache 让显存真降
                    continue
                self._components.pop(key, None)
                comp["module"] = None  # 断最后一个强引用,让 gc 回收 CUDA 存储
                freed = True
                logger.info(
                    "component L1 释放 role=%s file=%s(refs 空+非常驻)",
                    comp.get("role"), _basename(key[0]))
        return freed

    # --- RAM stash(spec 2026-06-12-ram-stash-eviction)---------------------------
    #
    # 驱逐/释放默认「挪内存留池」而非销毁(借鉴 ComfyUI ModelPatcher offload_device):
    # stash = module.to("cpu") + 标记,键/身份不变;命中时 .to(键里的卡) 搬回(秒级,
    # 免磁盘冷载)。RAM 水位(psutil)守住:不够不 stash;stash 池超水位按最旧销毁。
    # torchao fp8 量化权重(Float8Tensor)cpu↔cuda 往返 bit 一致已 spike 验证(2026-06-12)。

    @staticmethod
    def _stash_ram_reserve_bytes() -> int:
        import os  # noqa: PLC0415
        return int(float(os.getenv("NOUS_STASH_RAM_RESERVE_GB", "24")) * 1e9)

    def _stash_component(self, comp: dict) -> bool:
        """组件挪 CPU 留池(成功 True)。RAM 余量不足水位 / 搬运失败 → False(调用方走销毁)。"""
        if comp.get("stashed"):
            return True
        try:
            import psutil  # noqa: PLC0415
            need = self._component_bytes(comp["key"][0])
            if psutil.virtual_memory().available - need < self._stash_ram_reserve_bytes():
                logger.info("component stash 跳过(RAM 水位不足)role=%s file=%s",
                            comp.get("role"), _basename(comp["key"][0]))
                return False
            comp["module"].to("cpu")
            comp["stashed"] = True
            comp["stashed_at"] = time.monotonic()
            comp["stash_bytes"] = need
            # PR-4:原地页锁定(cudaHostRegister)→ restore 走 DMA 真异步(2.8× 带宽,
            # 真机 spike 19.4→53.3 GB/s)。预算/subclass/失败自动降级 pageable(无害)。
            from src.services.inference.pinned_stash import pin_module_inplace  # noqa: PLC0415
            comp["pin_regs"] = pin_module_inplace(comp["module"])
            logger.info("component L1 stash → RAM role=%s file=%s(~%dMB,命中秒回)",
                        comp.get("role"), _basename(comp["key"][0]), need // (1024 * 1024))
            self._trim_stash_lru()
            return True
        except Exception as e:  # noqa: BLE001 — stash 失败不挡释放主流程
            logger.warning("component stash 失败,回退销毁:%s", e)
            return False

    def _restore_component(self, comp: dict) -> None:
        """stashed 组件搬回键里的卡(身份卡)。失败抛 —— 调用方按组件加载失败处理。"""
        if not comp.get("stashed"):
            return
        dev = comp["device"]
        t0 = time.monotonic()
        regs = comp.pop("pin_regs", None)
        if regs:  # 非空才走快路;空列表 = 没 pin 上(CI 无 GPU/预算外),整体 .to() 即可
            # PR-4 快路:per-tensor non_blocking 批量 issue + 单次 synchronize
            #(pinned 源 DMA;subclass 同步兜底;函数内完成 unpin)。
            from src.services.inference.pinned_stash import restore_module_fast  # noqa: PLC0415
            restore_module_fast(comp["module"], dev, regs)
        else:
            comp["module"].to(dev)
        comp["stashed"] = False
        comp.pop("stash_bytes", None)
        logger.info("component L1 restore ← RAM role=%s file=%s dev=%s(%.1fs)",
                    comp.get("role"), _basename(comp["key"][0]), dev, time.monotonic() - t0)

    def _trim_stash_lru(self, extra_need: int = 0) -> None:
        """stash 池超 RAM 水位 → 按 stashed_at 最旧销毁,直至回水位内。

        extra_need(spec ram-pinned-linkage PR-2):即将额外占用的字节(如流式预 pin),
        要求腾到「available - extra_need ≥ reserve」—— 主动给马上要 pin 的权重腾地方,
        而非等占用发生后才补救。默认 0 = 旧行为(仅腾到水位线)。"""
        try:
            import psutil  # noqa: PLC0415
            while psutil.virtual_memory().available - extra_need < self._stash_ram_reserve_bytes():
                stashed = [(k, c) for k, c in self._components.items() if c.get("stashed")]
                if not stashed:
                    return
                key, comp = min(stashed, key=lambda kc: kc[1].get("stashed_at", 0.0))
                regs = comp.pop("pin_regs", None)
                if regs:
                    from src.services.inference.pinned_stash import unpin  # noqa: PLC0415
                    unpin(regs)  # 必须在 module 释放前注销(防悬挂注册)
                self._components.pop(key, None)
                comp["module"] = None
                logger.info("component stash LRU 销毁(RAM 水位)role=%s file=%s",
                            comp.get("role"), _basename(key[0]))
        except Exception:  # noqa: BLE001 — 水位裁剪 best-effort
            pass

    # 组件 kind(扫描器/节点用)→ L1 role(pipeline 子模块名)。
    _KIND_TO_ROLE = {"diffusion_models": "transformer", "clip": "text_encoder", "vae": "vae"}

    async def preload_image_component(self, spec, resident: bool = False, arch: str = "flux2") -> dict:
        """单组件预加载进 L1 池(引擎库「预加载/常驻」,spec §4)—— 不经 combo,独立 build。

        与 `_get_or_build_image_component` 区别:无 combo 引用(refs 空),由引擎库主动发起。
        repo 从 `arch` 反推(`_reference_repo_for_arch`,单组件没有别的两件套推 repo;clip/vae 的
        ComponentSpec 不带 adapter_arch,故 arch 单独传 —— diffusion_models 优先用 spec.adapter_arch)。
        offload 恒 none(预加载/常驻的组件全程在卡)。已在池 → 仅按需升 resident。
        返回 {state, key, role}。失败抛(调用方 runner handler 兜底,不崩)。
        """
        from src.services.inference.image_modular import (  # noqa: PLC0415
            build_bridged_text_encoder,
            build_bridged_transformer,
            build_bridged_vae,
        )
        role = self._KIND_TO_ROLE.get(spec.kind)
        if role is None:
            raise ValueError(f"不支持预加载该组件 kind={spec.kind!r}(仅 diffusion_models/clip/vae)")
        build_fn = {
            "transformer": build_bridged_transformer,
            "text_encoder": build_bridged_text_encoder,
            "vae": build_bridged_vae,
        }[role]
        # device='auto' → 解析具体卡(与 combo 路径一致,避免落 'auto' key)
        resolved_spec = self._resolve_component_device(spec)
        load_device = resolved_spec.device
        key = self._l1_component_key(resolved_spec, load_device)
        state_key = self._state_key_from_l1(key)  # 单一来源,与存进 dict 的一致
        comp = self._components.get(key)
        if comp is not None and comp.get("stashed"):
            try:
                await asyncio.to_thread(self._restore_component, comp)
            except Exception as e:  # noqa: BLE001
                logger.warning("component restore 失败(预加载),出池重建:%s", e)
                regs = comp.pop("pin_regs", None)
                if regs:
                    from src.services.inference.pinned_stash import unpin  # noqa: PLC0415
                    unpin(regs)
                self._components.pop(key, None)
                comp = None
        if comp is not None:
            if resident and not comp["resident"]:
                comp["resident"] = True
                logger.info("component L1 预加载命中升常驻 role=%s file=%s",
                            role, _basename(resolved_spec.file))
            comp["last_used"] = time.monotonic()
            return {"state": "loaded", "key": state_key, "role": role, "resident": comp["resident"]}
        eff_arch = getattr(resolved_spec, "adapter_arch", None) or arch or "flux2"
        repo = _reference_repo_for_arch(eff_arch) or _modular_repo_from_components({spec.kind: resolved_spec})
        logger.info("component L1 预加载 role=%s file=%s dev=%s resident=%s — building",
                    role, _basename(resolved_spec.file), load_device, resident)
        module = await asyncio.to_thread(build_fn, resolved_spec, repo, load_device)
        self._components[key] = {
            "module": module, "role": role, "key": key,
            "state_key": state_key,
            "refs": set(), "resident": resident,
            "last_used": time.monotonic(), "device": load_device,
        }
        return {"state": "loaded", "key": state_key, "role": role, "resident": resident}

    async def _get_or_load_modular_adapter(self, resolved, combo_key, pipeline_class, target, _emit, offload: str = "none", comp_devices: dict | None = None, comp_offloads: dict | None = None, stream_low_ram: bool = False):
        """图像引擎:建/复用 ModularImageBackend(class 名是历史,实际是标准 diffusers pipeline)。

        与 legacy 共用 `_image_adapters` combo 缓存 + 四态事件(coarse:加载前 loading、
        建好 loaded;细粒度 ComponentsManager 事件留后续)。comfy 单文件量化 → PR-2。
        重 load 经 `asyncio.to_thread` 不阻塞 runner 事件循环。
        """
        from src.services.inference.image_modular import ModularImageBackend

        model_id = self._derive_image_model_id(combo_key)
        async with self._lock_for(model_id):
            # cache hit → touch LRU + return adapter from unified _models dict
            entry = self._models.get(model_id)
            if entry is not None and entry.stashed:
                try:
                    t0 = time.monotonic()
                    await asyncio.to_thread(entry.adapter.restore)
                    entry.stashed = False
                    logger.info("adapter restore ← RAM id=%s(%.1fs)",
                                model_id, time.monotonic() - t0)
                except Exception as e:  # noqa: BLE001 — restore 失败按缓存失效处理,走重建
                    logger.warning("adapter restore 失败,销毁重建 id=%s:%s", model_id, e)
                    try:
                        entry.adapter.unload()
                    except Exception:  # noqa: BLE001
                        pass
                    self._models.pop(model_id, None)
                    entry = None
            if entry is not None:
                logger.info("image adapter HIT id=%s (modular)", model_id)
                entry.touch()
                for k in ("diffusion_models", "clip", "vae"):
                    await _emit(resolved[k], "loaded")
                return entry.adapter
            # cache miss — dump combo_key 字段,方便复盘是哪个字段导致不复用
            logger.info(
                "image adapter MISS id=%s (modular) combo=%s",
                model_id, self._explain_image_combo_key(combo_key),
            )

            repo = _modular_repo_from_components(resolved)
            for k in ("diffusion_models", "clip", "vae"):
                await _emit(resolved[k], "loading")
            try:
                # 单文件桥接 override:repo 外的单文件组件各自桥接(dequant + from_config of repo);
                # HF-layout 组件(diffusers/<m>/.../config.json)则 None,由 from_pretrained 加载。
                from src.services.inference.image_modular import (  # noqa: PLC0415
                    build_bridged_text_encoder,
                    build_bridged_transformer,
                    build_bridged_vae,
                )
                # PR-D / PR-D2 + 逐组件选卡/offload(2026-06-04):桥接 load_device 按**各组件自己的
                # offload**(非整管线 offload)——
                #   offload=cpu  → 'cpu'(forward 时 hook 挪到 compute 卡)
                #   offload=cuda:N → 'cuda:N'(stash 卡;hook forward 时挪到 compute 卡)
                #   offload=none → 各组件自己解析出的卡(整模型单卡时三者 == target;逐组件跨卡各落各卡)。
                def _comp_offload(role_key: str) -> str:
                    return getattr(resolved[role_key], "offload", "none") or "none"

                def _load_device_for(role_key: str) -> str:
                    o = _comp_offload(role_key)
                    if o in ("cpu", "stream"):
                        # stream:单文件桥接组件先建在 CPU,_apply_stream_offload 再挂 group offloading
                        # (offload_device=cpu)逐块流式 —— 否则双 DiT bf16 直建 GPU(37G)在小卡 OOM
                        # (2026-06-13:单文件 ideogram4 双 DiT stream 塞 24G 真机逮到)。
                        return "cpu"
                    if o.startswith("cuda:"):
                        return o
                    return resolved[role_key].device
                # **L1 池化安全闸**:逐组件跨卡 / 逐组件 offload 路径会给模块挂 pipe-specific
                # forward hook(_place_components_per_device)→ 跨 combo 共享同一被 hook 的模块
                # 会冲突(不同锚点/double-hook)。**只有整模型单卡且全 offload=none(plain `pipe.to`,
                # 无 per-component hook)才允许池化**;其余路径强制各自 build(传 sentinel 'hetero',
                # `_get_or_build_image_component` 据 offload!=none 不入池)。
                _same_card = all(
                    (comp_devices or {}).get(r, target) == target
                    for r in ("transformer", "text_encoder", "vae"))
                _all_none = all(
                    (comp_offloads or {}).get(r, "none") == "none"
                    for r in ("transformer", "text_encoder", "vae"))
                _poolable = _same_card and _all_none and offload == "none"

                def _pool_arg(role_key: str) -> str:
                    return "none" if _poolable else "hetero"
                # 逐组件 L1:命中复用、未命中 build + 存池(仅可池化路径)。combo miss 但组件
                # L1 命中 = 部分复用(用户场景:A、B 共用 X-bf16+vaeZ,只各自 build 不同的 clip)。
                t_ov = c_ov = v_ov = None
                if _is_standalone_single_file(resolved["diffusion_models"]):
                    t_ov = await self._get_or_build_image_component(
                        "transformer", build_bridged_transformer, resolved["diffusion_models"],
                        repo, _load_device_for("diffusion_models"), _pool_arg("diffusion_models"), model_id)
                if _is_standalone_single_file(resolved["clip"]):
                    c_ov = await self._get_or_build_image_component(
                        "text_encoder", build_bridged_text_encoder, resolved["clip"],
                        repo, _load_device_for("clip"), _pool_arg("clip"), model_id)
                if _is_standalone_single_file(resolved["vae"]):
                    v_ov = await self._get_or_build_image_component(
                        "vae", build_bridged_vae, resolved["vae"],
                        repo, _load_device_for("vae"), _pool_arg("vae"), model_id)
                # Ideogram-4 第二 DiT(unconditional,非对称 CFG,spec 2026-06-12):cond DiT spec 携带
                # unconditional_file → 用 build_bridged_transformer(config_sub=unconditional_transformer)直接建。
                # 不走 L1 池(uncond 恒与 cond 同 combo 配对,池化无收益);跟 cond DiT 同卡。
                tu_ov = None
                _uncond_file = getattr(resolved["diffusion_models"], "unconditional_file", None)
                if _uncond_file and _is_standalone_single_file(resolved["diffusion_models"]):
                    uncond_spec = resolved["diffusion_models"].model_copy(update={"file": _uncond_file})
                    tu_ov = await asyncio.to_thread(
                        build_bridged_transformer, uncond_spec, repo,
                        _load_device_for("diffusion_models"), "unconditional_transformer")
                def _build_adapter():
                    return ModularImageBackend(
                        repo=repo,
                        device=target,
                        dtype=resolved["diffusion_models"].dtype,
                        pipeline_class=pipeline_class,
                        offload=offload,
                        transformer_override=t_ov,
                        text_encoder_override=c_ov,
                        vae_override=v_ov,
                        unconditional_transformer_override=tu_ov,
                        comp_devices=comp_devices,
                        comp_offloads=comp_offloads,
                        stream_low_ram=stream_low_ram,
                    )

                # PR-D4 OOM 重试一次:加载 / _ensure_pipe 抛 CUDA OOM →
                # evict 同卡 LRU(可能是上次跑剩下的旧 image adapter)→ 重试。
                # 跟 get_or_load(L429-L438)的 retry 套路一致,但作用于 image。
                gpu_idx_target = int(target.split(":")[1]) if target.startswith("cuda:") else None
                try:
                    adapter = _build_adapter()
                    await adapter.load(target)
                    await asyncio.to_thread(adapter._ensure_pipe)
                except Exception as e:  # noqa: BLE001
                    if self._is_oom(e) and gpu_idx_target is not None:
                        evicted = await self.evict_lru(gpu_index=gpu_idx_target)
                        if evicted is not None:
                            logger.info(
                                "image adapter %r: OOM on first load, evicted %r on gpu %s, retrying",
                                model_id, evicted, gpu_idx_target,
                            )
                            try:
                                adapter = _build_adapter()
                                await adapter.load(target)
                                await asyncio.to_thread(adapter._ensure_pipe)
                            except Exception as e2:  # noqa: BLE001
                                for k in ("diffusion_models", "clip", "vae"):
                                    await _emit(resolved[k], "failed", f"{type(e2).__name__}: {e2}")
                                raise
                        else:
                            for k in ("diffusion_models", "clip", "vae"):
                                await _emit(resolved[k], "failed", f"OOM and nothing evictable: {e}")
                            raise
                    else:
                        for k in ("diffusion_models", "clip", "vae"):
                            await _emit(resolved[k], "failed", f"{type(e).__name__}: {e}")
                        raise
            except Exception as e:  # noqa: BLE001
                # 桥接 build_bridged_* / 其它非 OOM 路径 — 兜底 emit failed 再抛。
                # 内层 OOM 重试块已经发了 emit failed,这里只 cover 它没覆盖的路径。
                # combo 失败前可能已 L1 存了部分组件(如 transformer 建好但 clip 崩)——
                # combo 不入 _models 就永远不会 unload 释放这些 refs → 泄漏。这里主动回收
                # 此 model_id 的组件 refs(refs 空 → 出池),避免半建组件常驻显存。
                self._release_combo_components(model_id)
                for k in ("diffusion_models", "clip", "vae"):
                    await _emit(resolved[k], "failed", f"{type(e).__name__}: {e}")
                raise
            for k in ("diffusion_models", "clip", "vae"):
                await _emit(resolved[k], "loaded")
            gpu_idx = int(target.split(":")[1]) if target.startswith("cuda:") else 0
            self._models[model_id] = LoadedModel(
                spec=self._synthesize_image_spec(
                    model_id=model_id,
                    adapter_class_path="src.services.inference.image_modular.ModularImageBackend",
                    target_device=target,
                    vram_mb=self._estimate_image_vram_mb(resolved) or 0,
                    pipeline_class=pipeline_class,
                    source_files=[resolved[k].file for k in ("diffusion_models", "clip", "vae")],
                ),
                adapter=adapter,
                gpu_index=gpu_idx,
                gpu_indices=[gpu_idx],
            )
            return adapter
