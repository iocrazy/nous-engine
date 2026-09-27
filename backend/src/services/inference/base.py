"""InferenceAdapter v2 — unified typed surface for all modalities.

PR-0 establishes the v2 contract; existing 7 adapters (vLLM/SGLang/5 TTS)
all migrate to this shape in the same commit. No v1 coexistence.

Concrete subclasses pin `modality` to a specific MediaModality, accept
`paths: dict[str, str]` (LLM/TTS: just `paths['main']`), implement
`infer(req)` with a typed Request subclass, and may override
`infer_stream(req)` for SSE/streaming.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from enum import Enum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field


# ------------------------------------------------------------------
# Modality discriminator
# ------------------------------------------------------------------


class MediaModality(str, Enum):
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"
    EMBEDDING = "embedding"
    MULTIMODAL = "multimodal"


# ------------------------------------------------------------------
# Typed request schemas (pydantic v2 discriminated union)
# ------------------------------------------------------------------


class InferenceRequest(BaseModel):
    """Base for typed inference requests.

    Subclasses MUST override `modality` as Literal[MediaModality.X]
    so pydantic's discriminated-union resolver dispatches JSON
    payloads to the correct subclass.
    """

    request_id: str = Field(..., description="Caller trace id")
    timeout_s: float | None = Field(None, gt=0)
    modality: MediaModality  # subclasses narrow to Literal[X]


class Message(BaseModel):
    """Multimodal-capable chat message (OpenAI chat completions schema)."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]]


class TextRequest(InferenceRequest):
    modality: Literal[MediaModality.TEXT] = MediaModality.TEXT
    messages: list[Message]
    model: str = ""  # opaque tag for upstream OpenAI-compat servers
    max_tokens: int = Field(512, gt=0)
    temperature: float = Field(0.7, ge=0, le=2)
    stream: bool = False
    enable_thinking: bool = False
    api_key: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class LoRASpec(BaseModel):
    """LoRA reference by display name (ComfyUI-style)."""

    name: str
    strength: float = Field(1.0, ge=-2, le=2)
    # PR-4: component path carries the absolute LoRA file path so the runner
    # can load it without a name→path registry lookup (from_components sets
    # _lora_paths={}). Legacy yaml path leaves this None and resolves via
    # _lora_paths[name] as before.
    path: str | None = None


class ImageRequest(InferenceRequest):
    """图像请求 schema。

    2026-09-26 自建图像引擎(Modular Diffusers / Anima / SeedVR2 / LCS)已物理删除,出图只走
    ComfyUI 桥;代码存档见 tag image-engine-native-final。本类仅作为 runner fake adapter 与
    schema 测试的请求类型保留。下方字段注释描述的是已删引擎当年的消费方式,留作历史说明。
    """

    modality: Literal[MediaModality.IMAGE] = MediaModality.IMAGE
    prompt: str
    negative_prompt: str = ""
    width: int = Field(1024, ge=64, le=4096)
    height: int = Field(1024, ge=64, le=4096)
    steps: int = Field(25, ge=1, le=200)
    seed: int | None = None
    cfg_scale: float = Field(7.0, ge=0, le=30)
    # 一次出图张数(batch,对齐 ComfyUI batch_size / OpenAI n)。N>1 经 diffusers
    # num_images_per_prompt 一次前向出 N 张(共享 prompt,seed 派生 N 个噪声)→ 结果首张进 data、
    # 其余进 extra_images。**显存按 N 近似线性涨**(同一前向的 batch 维),大模型大图慎调高。
    # 标准 pipe 走 num_images_per_prompt;段路(非 euler 采样器手写分段循环)走 prepare_latents(N) 同样出
    # N 张;仅续采/接力(init_latent)保持 1(batch 接力语义复杂)。
    num_images: int = Field(1, ge=1, le=8)
    # 采样控制(复刻 ComfyUI KSampler 两下拉,映射 diffusers flow-match scheduler):
    # sampler_name = scheduler 类(euler→FlowMatchEulerDiscrete / heun→Heun / lcm→LCM);
    # scheduler = sigma 调度(normal=默认 / karras / exponential / beta → use_*_sigmas)。
    sampler_name: str = "euler"
    scheduler: str = "normal"
    # img2img 降噪强度(0..1):**仅当架构注册了 img2img 变体(z-image)且连了 input_image** 时生效
    # (spec 2026-06-08-multi-sampling-cross-model PR-A2)。1.0 = 全量去噪 ≈ 忽略输入图(等同纯文生图/
    # 参考编辑,零回归);<1 = 从输入图 VAE-encode 后加噪到该比例再去噪(同模型加噪重去噪 refine)。
    # 无 img2img 变体的架构(Flux2-Klein 走多参考编辑条件)忽略此值。
    strength: float = Field(1.0, ge=0, le=1)
    # PR-D:权重 offload 目标(none = 不 offload;cpu = enable_model_cpu_offload 把不用的组件挪 CPU,慢
    # 但让大模型塞进小显存,如 True-v2 bf16 进 3090 24GB)。cuda:N 跨卡 offload 留 PR-D2。
    offload: str = "none"
    loras: list[LoRASpec] = Field(default_factory=list)
    # 输入图(编辑/img2img):本地磁盘路径或 base64 data URI。None = 纯文生图(零回归)。
    # 编辑类架构(Flux2 多参考编辑 / Qwen-Image-Edit)消费;引擎按 pipeline 是否支持 image=
    # 决定是否注入(当年由已删的 model_arch_adapter 判定)。
    # (2026-09-26:runner 侧把签名 URL 解析成本地路径的那段已随图像派发删除。)
    input_image: str | None = None
    # 输出模式(spec 2026-06-08 路 B,PR-B1):"image"=终端 VAE decode 出图(默认,字节零回归);
    # "latent"=终端不 decode,把真 latent 张量落盘,返 latent_ref 描述符(同空间真 latent 接力用,
    # 下游 sample_from_latent 注入 latents= 接续去噪)。引擎据此对 pipe 传 output_type="latent"。
    output_mode: str = "image"
    # 留噪 latent 接力 / 分段采样(spec 2026-06-08 路 B,PR-B2;对齐 ComfyUI KSamplerAdvanced)。
    # 把一次 N 步去噪劈成多段(不同 LoRA/cfg/sampler),中途带噪 latent 直接交接(不过 VAE、不重加噪)。
    # 任一字段非默认 → 引擎走「手写分段去噪循环」(_run_zimage_segmented),否则走整段 pipe()(零回归)。
    #   start_at_step:从全 schedule 的第几步起(续采段;refiner 配 init_latent_ref + add_noise=False)。
    #   end_at_step:跑到第几步停(None=跑到底);<steps 时停在带噪水平(base 段,配 return_with_leftover_noise)。
    #   add_noise:注入 init_latent 时是否重加噪(False=原样续采=留噪接力;True=按 start sigma 加噪=img2img 风)。
    #   return_with_leftover_noise:base 段停步时是否保留余噪(True=带噪交接;False=force_full_denoise 末步去到底)。
    #   init_latent_ref:上段导出的 latent_ref(本地路径 + arch/latent_channels 元信息),引擎读回注入 latents=。
    #     派发前校验 arch/latent_channels 与本段模型一致,不符 → 人话报错(对齐 anima arch-mismatch 校验)。
    start_at_step: int = Field(0, ge=0, le=200)
    end_at_step: int | None = Field(None, ge=0, le=200)
    add_noise: bool = True
    return_with_leftover_noise: bool = False
    init_latent_ref: dict | None = None
    # 采样期 latent 干预(复刻 comfyui-lcs post-CFG hook,spec 2026-06-10-sampling-intervention-hook):
    # 每个描述符 = {"_type": "lcs_sharpness"|"lcs_color_anchor"|"test_shift", "strength":.., "start_step":..,
    # "end_step":.., "calib_ref": {"path": <safetensors>}}。引擎 _build_interventions 据此构 per-step hook
    # (denoised x0 语义点逐步改 latent)。**不落 tensor 进此字段**(标定数据落盘 safetensors,只带 path,
    # 同 init_latent_ref 铁律)。None/空 = 无干预(零回归)。
    interventions: list[dict] | None = None


class AudioRequest(InferenceRequest):
    modality: Literal[MediaModality.AUDIO] = MediaModality.AUDIO
    text: str
    voice: str = "default"
    speed: float = Field(1.0, gt=0, le=4)
    sample_rate: int = 24000
    reference_audio: str | None = None
    reference_text: str | None = None
    emotion: str | None = None
    format: Literal["wav", "mp3", "ogg"] = "wav"


class VideoRequest(InferenceRequest):
    """V0 schema-only placeholder. No backend implementation."""

    modality: Literal[MediaModality.VIDEO] = MediaModality.VIDEO
    prompt: str
    duration_s: float = Field(4.0, gt=0, le=30)
    fps: int = Field(24, ge=1, le=60)
    width: int = 1280
    height: int = 720


# ------------------------------------------------------------------
# Result envelope
# ------------------------------------------------------------------


class UsageMeter(BaseModel):
    """Cross-modality usage counter. Each adapter fills what applies."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    audio_seconds: float | None = None
    image_count: int | None = None
    video_seconds: float | None = None
    latency_ms: int


class StageTimings(BaseModel):
    """Stage-level timing for observability + post-mortem.

    Each adapter fills the fields that apply:
      LLM:   connect_ms, first_token_ms (TTFT), sample_ms, decode_ms
      Image: encode_ms, denoise_ms, vae_ms
      Audio: encode_ms, synthesize_ms
    """

    connect_ms: int | None = None
    first_token_ms: int | None = None
    encode_ms: int | None = None
    sample_ms: int | None = None
    decode_ms: int | None = None
    denoise_ms: int | None = None
    vae_ms: int | None = None
    synthesize_ms: int | None = None


class InferenceResult(BaseModel):
    """Unified result envelope across all modalities."""

    media_type: str  # "application/json" | "audio/wav" | "image/png" | ...
    data: bytes
    # batch 出图(num_images>1)的额外张(第 1 张在 data,其余在此)。加法字段,单图消费者读 data 不变。
    extra_images: list[bytes] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    usage: UsageMeter

    model_config = {"arbitrary_types_allowed": True}


class StreamEvent(BaseModel):
    """Stream event for `infer_stream`. Flat envelope; payload is opaque."""

    type: Literal["progress", "delta", "done", "error"]
    payload: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------
# ABC
# ------------------------------------------------------------------


class InferenceAdapter(ABC):
    """Adapter ABC.

    Concrete subclasses:
      - declare `modality: ClassVar[MediaModality]`
      - declare `estimated_vram_mb: ClassVar[int]`
      - implement `__init__(paths: dict[str, str], device: str = "cuda", **params)`
        — `paths['main']` is the primary file/dir for single-component models;
          image-class adapters read `paths['transformer']`, `paths['text_encoder']`,
          `paths['vae']`
      - implement `load(device)` and `infer(req)`
      - optionally override `infer_stream(req)` for SSE/streaming
        (presence detected via `supports_streaming()` classmethod —
         single source of truth, no separate flag to keep in sync)
    """

    modality: ClassVar[MediaModality] = MediaModality.MULTIMODAL
    estimated_vram_mb: ClassVar[int] = 0
    # True = 这个适配器接受 manager 传下来的 GPU 组(`gpus=[0, 2]`,张量并行)。
    # 默认 False:大多数适配器只会用一张卡,给它分组会造成"manager 按两张卡预留、
    # 实际单卡跑"的幻影预留 + loaded_gpus 撒谎(2026-09-03 审查)。API 也据此拒绝
    # 给不支持的引擎配组。
    supports_gpu_group: ClassVar[bool] = False

    def __init__(self, paths: dict[str, str], device: str = "cuda", **params: Any):
        self.paths = paths
        self.device = device
        self._model: Any = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @classmethod
    def supports_streaming(cls) -> bool:
        """True iff subclass overrides `infer_stream`. Derived — no flag."""
        return cls.infer_stream is not InferenceAdapter.infer_stream

    @abstractmethod
    async def load(self, device: str) -> None:
        """Load model weights onto the given device."""

    def unload(self) -> None:
        """Release model from memory."""
        self._model = None

    def stash(self) -> bool:
        """RAM stash(spec 2026-06-12):权重挪 CPU 待命,腾显存但保留实例(命中 restore
        秒回)。默认不支持(False)—— 调用方回退销毁;支持的引擎(ModularImageBackend)覆写。"""
        return False

    def restore(self) -> None:
        """stash 的权重搬回原卡。默认 no-op(配合 stash()=False)。"""

    @abstractmethod
    async def infer(self, req: InferenceRequest) -> InferenceResult:
        """Run inference with a typed request."""

    async def infer_stream(
        self, req: InferenceRequest
    ) -> AsyncIterator[StreamEvent]:
        """Streaming inference. Default raises so non-streaming adapters
        signal "not supported" via `supports_streaming() == False`."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement infer_stream"
        )
        if False:  # pragma: no cover  — satisfies AsyncIterator protocol
            yield  # type: ignore[unreachable]

