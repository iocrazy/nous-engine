"""VLLMAdapter — manages vLLM as a subprocess with full lifecycle control."""
from __future__ import annotations

import asyncio
import contextlib
import json as _json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from src.services.inference.base import (
    InferenceAdapter,
    InferenceRequest,
    InferenceResult,
    MediaModality,
    StreamEvent,
    TextRequest,
    UsageMeter,
)
from src.services.inference._placement import (
    LaunchPlacement,
    apply_visible_devices,
    estimate_model_size_gb,
    resolve_launch_placement,
)
from src.utils.constants import ALLOWED_LLM_HOSTS

logger = logging.getLogger(__name__)


def _pid_cmdline_is_vllm(pid: int) -> bool:
    """True iff /proc/<pid> exists and its cmdline still looks like a vLLM process.

    Guards against recycled-PID friendly fire: an *adopted* orphan PID may have
    died (e.g. after the GPU falls off the bus, Xid 79) and been reused by the OS
    for an unrelated process — a proxy daemon, an sshd session, or `systemd --user`.
    killpg() on such a stale PID would take down innocent process groups. Always
    re-verify identity immediately before signalling an externally-sourced PID.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmdline = fh.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    return b"vllm" in cmdline.lower()


def clamp_util_to_free(utilization: float, gpu_free_gb: float, gpu_total_gb: float) -> float:
    """把 gpu_memory_utilization 压到所选卡当前 free 以内(留 0.5G 余量)。

    vLLM 的 utilization 以 total 为分母、按「整卡归它」假设 claim 显存;
    resolve_vram_utilization 的三条路(overlay 预算/yaml/auto 公式)也都不看 free。
    卡上已有别的进程时预算超过 free → 启动期 CUDA OOM、exit 1、日志截断难查
    (2026-07-05 GPU2 上 22G 预算/22.5G free 启动即炸)。下限 0.05:free 见底时
    也给 vLLM 一个合法值,让它自己报「显存不足」而不是这里除零/负数。
    """
    if gpu_total_gb <= 0:
        return utilization
    cap = max(0.05, (gpu_free_gb - 0.5) / gpu_total_gb)
    if utilization > cap:
        logger.warning(
            "gpu_memory_utilization %.2f exceeds free VRAM cap %.2f "
            "(free=%.1fG/total=%.1fG) — clamping to avoid startup OOM",
            utilization, cap, gpu_free_gb, gpu_total_gb,
        )
        return round(cap, 4)
    return utilization


# ---------------------------------------------------------------------------
# vllm_args:模型 yaml 的 `params.vllm_args` 透传任意 vLLM 长参数
# ---------------------------------------------------------------------------
# 动机:适配器只自己拼固定的那几个参数(tp / max_model_len / quantization / dtype /
# max_num_seqs / prefix-caching / runner)。vLLM 还有几十个有用的开关(MTP 投机解码的
# --speculative-config、把思考分离到 reasoning_content 的 --reasoning-parser……),
# 以前只能手起进程,经 nous 加载的实例享受不到。`params.vllm_args` 就是那个口子。

#: 适配器自己会拼的参数。vllm_args 里出现同名 → **以 vllm_args 为准**,把适配器那份
#: 从 argv 里摘掉(同一个 flag 出现两次 vLLM 会报错/静默取一个,都不是确定行为),
#: 并 warning 说明谁盖了谁。
VLLM_ADAPTER_OWNED_FLAGS = frozenset({
    "--quantization",
    "--max-model-len",
    "--gpu-memory-utilization",
    "--max-num-seqs",
    "--limit-mm-per-prompt",
    "--enable-auto-tool-choice",
    "--tool-call-parser",
    "--runner",
    "--dtype",
    "--enable-prefix-caching",
})

#: 安全边界:这几个**不许**出现在 vllm_args 里,给了直接 ValueError(不是覆盖)。
#: 模型路径 / 端口由 manager 决定(yaml 改掉 = 绕过 LOCAL_MODELS_PATH 加载任意目录、
#: 或占掉别人的端口);落卡与 tp 由 `_placement` 统一决定(见 CLAUDE.md 的三条不变式),
#: `--device` / `--tensor-parallel-size` 会让子进程的结论跟 CUDA_VISIBLE_DEVICES 脱钩。
VLLM_ARGS_FORBIDDEN: dict[str, str] = {
    "--model": "模型路径由 manager 按 LOCAL_MODELS_PATH 解析,yaml 不得改写",
    "--port": "端口由 manager 分配,yaml 不得改写",
    "--device": "落卡由 _placement 统一决定(CUDA_VISIBLE_DEVICES),yaml 不得干预",
    "--tensor-parallel-size": (
        "tp 由 GPU 组决定(hardware.yaml 的组 + 模型的 gpus 字段),不能在 vllm_args 里"
        "覆盖,否则会与 _placement 钉的卡数不一致导致启动失败。"
        "放置决策只在 ModelManager._resolve_placement 一处 —— tp 是放置结论的一部分,"
        "不是调优旋钮。要收窄 tp 请用 params.tensor_parallel_size,要换组请改 gpus"
    ),
}


def normalize_vllm_flag(key: str) -> str:
    """`speculative_config` / `speculative-config` / `--speculative-config`
    统一成 `--speculative-config`(下划线一律换连字符)。"""
    return "--" + str(key).strip().lstrip("-").replace("_", "-")


def render_vllm_args(vllm_args: dict | None) -> list[str]:
    """把 `params.vllm_args` 渲染成 argv 片段。

    - bool True  → 只加 `--flag`(**不是** `--flag true`,vLLM 的 store_true 不吃值)
    - bool False → 完全不加(等于没配这个开关)
    - dict/list  → `json.dumps` 成**一个**参数值(`--speculative-config` 就吃 JSON 串)
    - None       → 跳过(等于没配)
    - 其余       → `--flag value`

    安全边界内的键(`VLLM_ARGS_FORBIDDEN`)直接 ValueError。纯函数,好在 __init__ 里
    早失败,而不是等 load() 已经预留了显存才炸。
    """
    if not vllm_args:
        return []
    if not isinstance(vllm_args, dict):
        raise ValueError(f"vllm_args 必须是 dict,收到 {type(vllm_args).__name__}")
    out: list[str] = []
    for raw_key, value in vllm_args.items():
        flag = normalize_vllm_flag(raw_key)
        if flag in VLLM_ARGS_FORBIDDEN:
            raise ValueError(f"vllm_args 不接受 {flag}:{VLLM_ARGS_FORBIDDEN[flag]}")
        if value is None:
            continue
        if isinstance(value, bool):
            if value:
                out.append(flag)
            continue
        if isinstance(value, (dict, list)):
            out += [flag, _json.dumps(value, ensure_ascii=False, separators=(",", ":"))]
            continue
        if isinstance(value, (str, int, float)):
            out += [flag, str(value)]
            continue
        raise ValueError(
            f"vllm_args[{raw_key}] 的类型 {type(value).__name__} 不支持"
            "(只接受 str/int/float/bool/dict/list)"
        )
    return out


#: `vllm_args` 的值里可以写这个占位符,展开成**已解析的绝对模型目录**。
#: 给 `--chat-template` 这类「指向模型自带文件」的参数用(WeMM-Embedding 的
#: embedding_chat_template.jinja)。不用它就得把 LOCAL_MODELS_PATH 写死进 git 里的
#: yaml,破坏「重格/搬盘只改 .env 两个根」(model_roots.yaml 文件头)。
#: 展开在 `_build_command` 里做 —— 构造期 render 只做校验(早失败),那时还不知道
#: 模型路径解析成了 LOCAL_MODELS_PATH 下的相对路径还是 yaml 给的绝对路径。
MODEL_DIR_PLACEHOLDER = "{model_dir}"


def substitute_model_dir(argv: list[str], model_dir: str) -> list[str]:
    """把 argv 里的 `{model_dir}` 换成 `model_dir`。没有占位符则原样返回。

    只做字面替换,不碰其它花括号 —— `--speculative-config` 的 JSON 串
    (`{"method":"mtp"}`)不含这个占位符,不会被误伤。
    """
    if not any(MODEL_DIR_PLACEHOLDER in tok for tok in argv):
        return argv
    return [tok.replace(MODEL_DIR_PLACEHOLDER, model_dir) for tok in argv]


def merge_vllm_args(cmd: list[str], extra: list[str], *, label: str = "vLLM") -> list[str]:
    """把 vllm_args 渲染出的 `extra` 并进适配器拼好的 `cmd`。

    同名 flag 以 extra 为准:先把 cmd 里那一份(flag + 它的值)摘掉再追加,
    保证同一个参数只出现一次。
    """
    if not extra:
        return cmd
    incoming = {tok for tok in extra if tok.startswith("--")}
    collide = incoming & VLLM_ADAPTER_OWNED_FLAGS & set(cmd)
    for flag in sorted(collide):
        logger.warning(
            "%s: vllm_args 覆盖了适配器自己拼的 %s —— 以 yaml 的 vllm_args 为准",
            label, flag,
        )
    if not collide:
        return list(cmd) + extra
    out: list[str] = []
    i = 0
    while i < len(cmd):
        tok = cmd[i]
        if tok in collide:
            i += 1
            # 顺带吃掉它的值(下一个不以 `--` 开头的 token);store_true 类没有值
            if i < len(cmd) and not cmd[i].startswith("--"):
                i += 1
            continue
        out.append(tok)
        i += 1
    return out + extra


class VLLMAdapter(InferenceAdapter):
    """Adapter that spawns vLLM as a subprocess and manages its lifecycle.

    load()         → start vLLM subprocess → wait for health check
    unload()       → kill subprocess → free GPU memory
    infer(req)     → HTTP POST /v1/chat/completions, return InferenceResult
    infer_stream() → SSE stream → yields StreamEvent("delta"|"done")
    """

    modality = MediaModality.TEXT
    estimated_vram_mb = 0  # Determined at runtime
    # 接受 manager 传下来的 GPU 组(张量并行)。见 InferenceAdapter.supports_gpu_group。
    supports_gpu_group = True

    def __init__(
        self,
        paths: dict[str, str],
        device: str = "cuda",
        vllm_base_url: str | None = None,
        vllm_port: int | None = None,
        tensor_parallel_size: int | None = None,
        max_model_len: int | None = None,
        gpu_memory_utilization: float | None = None,
        quantization: str | None = None,
        dtype: str | None = None,
        max_num_seqs: int | None = None,
        enable_prefix_caching: bool | None = None,
        vllm_runner: str | None = None,
        vram_budget: dict | None = None,
        adopt_pid: int | None = None,
        gpus: list[int] | None = None,
        vllm_args: dict | None = None,
        **kwargs: Any,
    ):
        super().__init__(paths=paths, device=device)
        # Single-component model: 'main' is the HF model dir under LOCAL_MODELS_PATH
        self.model_path = Path(paths.get("main", ""))
        self._port = vllm_port or (int(vllm_base_url.split(":")[-1]) if vllm_base_url else 0)
        self._tp = tensor_parallel_size
        # 显式 GPU 组(模型级 `gpus: [0, 2]`)。给了就以它为准:
        # CUDA_VISIBLE_DEVICES=组内物理 index + --tensor-parallel-size=组内卡数
        # (除非显式 tensor_parallel_size 更小)。None = 走单卡 / 自动选组。
        self._gpus = [int(i) for i in gpus] if gpus else None
        # load() 时真正落地的组(含自动选出来的);观测/测试读它,None=单卡。
        self.gpu_indices: list[int] | None = None
        self._max_model_len = max_model_len
        self._gpu_mem_util = gpu_memory_utilization
        self._quantization = quantization
        self._max_num_seqs = max_num_seqs
        self._dtype = dtype
        # If True, vLLM is launched with --enable-prefix-caching.
        # Per-model override; reads from models.yaml `params` block.
        self._enable_prefix_caching = enable_prefix_caching
        # vLLM --runner(0.22):pooling = embedding/分类模型(Qwen3-Embedding 等),
        # 起的还是同一个 openai api_server,只是暴露 /v1/embeddings 而非 chat。
        # None/缺省 = 不传(vLLM auto,生成模型零回归)。models.yaml params 块透传。
        self._vllm_runner = vllm_runner
        # 任意 vLLM 长参数透传(models.d/<id>.yaml 的 `params.vllm_args`)。
        # **在这里就渲染**:yaml 写了安全边界内的键(--model/--port/--device)或不支持的
        # 值类型,在构造期就 ValueError,而不是等 load() 预留完显存、起了子进程才炸。
        self._vllm_args = dict(vllm_args) if vllm_args else {}
        self._vllm_extra_argv: list[str] = render_vllm_args(self._vllm_args)
        # 每模型显存预算({mode,value};spec 2026-06-13)—— runtime overlay 注入,加载时
        # 解析成 gpu_memory_utilization。优先级高于 models.yaml 的 gpu_memory_utilization。
        self._vram_budget = vram_budget
        # Port resolved lazily in load() if not set
        if self._port:
            self._base_url = f"http://localhost:{self._port}"
        else:
            self._base_url = None  # Will be set in load()
        self.base_url = self._base_url
        self._process: subprocess.Popen | None = None
        self._adopt_pid = adopt_pid  # PID of an orphan process to adopt
        self._adopted_pid: int | None = None  # Set in load() when adopting
        # trust_env=False:本 client 只连本地 vLLM 子进程(localhost:port)。默认
        # trust_env=True 会让 httpx 在请求时套用 HTTP_PROXY/ALL_PROXY env(本机 socks
        # 代理),把 localhost 调用经代理转发 → health/infer 失败或变慢(round3 #2;
        # 注:proxy=None 不够,env proxy 在请求期解析,只有 trust_env=False 彻底绕开)。
        self._client = httpx.AsyncClient(
            timeout=120, limits=httpx.Limits(max_connections=10), trust_env=False
        )
        self._managed = True  # True = we control the subprocess
        self.is_audio = False  # 设于 load():ASR/音频模型(走 /v1/audio/transcriptions)
        # round3 #1:vLLM 运行期持续往 stdout 打日志,Popen 的 PIPE(~64KB)填满后
        # 子进程 write 阻塞 = 推理服务冻结。后台 daemon 线程持续抽干进有界 deque。
        self._stdout_tail: deque[str] = deque(maxlen=200)
        self._drain_thread: threading.Thread | None = None

    def _auto_configure(self, device: str | None) -> dict:
        """Auto-calculate vLLM launch parameters based on model and GPU state."""
        import json
        from src.config import get_settings
        from src.services.gpu_monitor import poll_gpu_stats

        settings = get_settings()
        model_path = Path(settings.LOCAL_MODELS_PATH) / self.model_path
        if not model_path.exists():
            model_path = Path(self.model_path)

        # 1. Read model config.json
        config_file = model_path / "config.json"
        model_config: dict = {}
        if config_file.exists():
            with open(config_file) as f:
                model_config = json.load(f)

        # 2. 模型体积 —— 与 manager 共用同一把尺子(放置决策和预算必须基于同一个数)
        model_size_gb = estimate_model_size_gb(model_path)

        # 3. Auto-detect quantization from config
        quant_config = model_config.get("quantization_config", {})
        quantization = quant_config.get("quant_method")  # "gptq", "awq", "compressed-tensors", etc
        # Use gptq_marlin for faster inference (vLLM recommended over plain gptq)
        if quantization == "gptq":
            quantization = "gptq_marlin"

        # 4. Auto-detect dtype — let vLLM choose (bfloat16 is safer for mixed-dtype GPTQ models)
        dtype = None

        # 5-7. 落卡 / tp / 预算 —— **只执行 manager 传下来的 device/gpus,不自己选卡**。
        # 放置决策的唯一入口是 ModelManager._resolve_placement(2026-09-03 审查):
        # 适配器自行改 gpu_idx 会造成"预算按 A 卡算、CVD 钉 B 卡"的启动期 OOM,
        # 且 manager 记录的落卡与真实占用不符。
        placement = resolve_launch_placement(
            device=device, gpus=self._gpus, requested_tp=self._tp,
            stats=poll_gpu_stats(), label="vLLM",
        )
        tp = placement.tp
        group = list(placement.cards) if tp > 1 else None
        gpu_idx = placement.primary
        gpu_total_gb = placement.total_gb or 24.0
        gpu_free_gb = placement.free_gb or gpu_total_gb

        # 8. Calculate gpu_memory_utilization
        if tp > 1:
            per_gpu_model = model_size_gb / tp
            kv_buffer_gb = 4.0
            needed = per_gpu_model + kv_buffer_gb
            utilization = min(0.85, needed / gpu_total_gb)
        else:
            kv_buffer_gb = min(4.0, gpu_free_gb - model_size_gb - 1.0)
            if kv_buffer_gb < 1.0:
                kv_buffer_gb = 1.0
            needed = model_size_gb + kv_buffer_gb + 1.0  # +1GB CUDA overhead
            utilization = min(0.92, needed / gpu_total_gb)

        # 9. Calculate max_model_len based on available KV cache memory
        # KV cache per token varies by model, use ~128KB/token as estimate
        kv_bytes_per_token = 131072  # 128KB, typical for Qwen3.5/Gemma4 MoE
        kv_cache_bytes = kv_buffer_gb * 1024**3
        estimated_max = int(kv_cache_bytes / kv_bytes_per_token)
        # Read model's native max from config
        max_position = model_config.get("max_position_embeddings") or \
                       model_config.get("text_config", {}).get("max_position_embeddings", 262144)
        # Use the smaller of estimated capacity and model native max, round down to 1024
        max_model_len = min(estimated_max, max_position)
        max_model_len = max(2048, (max_model_len // 1024) * 1024)  # at least 2048

        # 10. Calculate max_num_seqs (conservative to avoid sampler warmup OOM)
        if kv_buffer_gb < 3.0:
            max_num_seqs = 16
        elif kv_buffer_gb < 6.0:
            max_num_seqs = 32
        else:
            max_num_seqs = 64

        # 11. Find free port
        port = self._port
        if not port:
            import socket
            with socket.socket() as s:
                s.bind(("", 0))
                port = s.getsockname()[1]

        # 12. Detect multimodal (vision-language) models
        archs = model_config.get("architectures") or []
        is_multimodal = any(
            "VL" in a or "Vision" in a or "Multimodal" in a or "Omni" in a
            for a in archs
        ) or model_config.get("vision_config") is not None

        # 12b. Detect audio/ASR models(音频进、文本出,如 Qwen3-ASR)。它们也是多模态,但
        # 输入是 audio 不是 image → 命令里要 --limit-mm-per-prompt {"audio":N} 而非 image。
        # arch 含 ASR/Audio/Whisper 或 config 有 audio_config/audio_encoder 即判定。
        is_audio = any(
            "ASR" in a or "Audio" in a or "Whisper" in a for a in archs
        ) or model_config.get("audio_config") is not None or model_config.get("audio_encoder_config") is not None

        return {
            "port": port,
            "tp": tp,
            # 最终落卡:多卡组(tp>1)时是整组,单卡时 None(单卡看 gpu_idx)。
            "gpus": list(group) if group else None,
            # 真正要钉进 CUDA_VISIBLE_DEVICES 的卡 —— 单卡也在里面,绝不为空。
            "cards": list(placement.cards),
            "max_model_len": max_model_len,
            "utilization": round(utilization, 2),
            "quantization": quantization,
            "dtype": dtype,
            "max_num_seqs": max_num_seqs,
            "gpu_idx": gpu_idx,
            "model_size_gb": round(model_size_gb, 2),
            "gpu_total_gb": round(gpu_total_gb, 2),
            "gpu_free_gb": round(gpu_free_gb, 2),
            "is_multimodal": is_multimodal,
            "is_audio": is_audio,
        }

    async def load(self, device: str | None = None) -> None:
        """Start vLLM subprocess or connect to existing instance."""
        # First check if vLLM is already running on this port
        if self._base_url and await self._health_check():
            self._model = True
            # 回填 max_model_len(关键):快速返回路径(重连存活 vLLM / adopt orphan)若不设,
            # _clamp_max_tokens 退回 4096 → backend 重启重连后长输出被静默砍到 ~3.5k。优先用
            # yaml 配的 _max_model_len,否则从运行中 vLLM 的 /v1/models 读。
            self.max_model_len = (
                self._max_model_len or await self._fetch_remote_max_model_len() or 4096)
            if self._adopt_pid:
                # Adopt orphan process — we manage its lifecycle
                self._managed = True
                self._adopted_pid = self._adopt_pid
                logger.info("Adopted orphan vLLM (pid=%d) at %s", self._adopt_pid, self._base_url)
            else:
                self._managed = False  # External instance, don't kill it
                logger.info("Connected to existing vLLM at %s", self._base_url)
            return

        # Auto-configure parameters
        auto = self._auto_configure(device)
        port = self._port or auto["port"]
        # tp / 落卡在 _auto_configure 里已经定死(resolve_launch_placement,含
        # "显式 tp 只能收窄组"和"没组就不许 tp>1"),这里**不再重算一遍**。
        tp = auto["tp"]
        cards: list[int] = list(auto.get("cards") or [])
        gpu_group: list[int] | None = list(auto["gpus"]) if auto.get("gpus") else None
        # 真实落卡回填给 manager(它 load 完读这个字段登记 gpu_indices)。
        self.gpu_indices = list(cards) if cards else None
        max_model_len = self._max_model_len or auto["max_model_len"]
        self.max_model_len = max_model_len  # expose for clamp logic
        # 显存预算优先级:overlay vram_budget(percent/absolute) > yaml gpu_memory_utilization > auto 公式
        from src.config import resolve_vram_utilization
        utilization = resolve_vram_utilization(
            self._vram_budget, auto["gpu_total_gb"], self._gpu_mem_util, auto["utilization"],
        )
        # 三条预算路径都不看 free —— 统一在此按所选卡实际剩余封顶,防启动期 OOM。
        # 用 .get 容忍精简的 auto dict(测试 patch / 老快照可能缺 gpu_*_gb 键);
        # total<=0 时 clamp 自身直接返回原值,不误伤。
        _total_gb = auto.get("gpu_total_gb", 0.0)
        utilization = clamp_util_to_free(
            utilization, auto.get("gpu_free_gb", _total_gb), _total_gb,
        )
        quantization = self._quantization or auto["quantization"]
        dtype = self._dtype or auto["dtype"]
        max_num_seqs = self._max_num_seqs or auto["max_num_seqs"]

        # Update base_url now that port is resolved
        self._port = port
        self._base_url = f"http://localhost:{port}"
        self.base_url = self._base_url

        logger.info(
            "Auto-config: model=%.1fGB, tp=%d, gpus=%s, max_len=%d, util=%.2f, seqs=%d, quant=%s",
            auto["model_size_gb"], tp, gpu_group or cards,
            max_model_len, utilization, max_num_seqs, quantization,
        )

        # Resolve model path
        from src.config import get_settings
        settings = get_settings()
        model_path = str(Path(settings.LOCAL_MODELS_PATH) / self.model_path)
        if not Path(model_path).exists():
            model_path = str(self.model_path)  # Try as absolute path

        # Build vLLM command
        cmd = [
            sys.executable, "-m", "vllm.entrypoints.openai.api_server",
            "--model", model_path,
            "--port", str(port),
            "--max-model-len", str(max_model_len),
            "--gpu-memory-utilization", str(utilization),
        ]
        if self._vllm_runner:
            cmd += ["--runner", self._vllm_runner]
        if tp > 1:
            cmd += ["--tensor-parallel-size", str(tp)]
        if quantization:
            cmd += ["--quantization", quantization]
        if dtype:
            cmd += ["--dtype", dtype]
        if max_num_seqs:
            cmd += ["--max-num-seqs", str(max_num_seqs)]
        if self._enable_prefix_caching:
            # Repeated system prompts / few-shot examples reuse cached KV
            # blocks instead of re-prefilling. Memory cost is tiny metadata;
            # benefit is large when callers send the same prefix often.
            cmd += ["--enable-prefix-caching"]
        if auto.get("is_audio"):
            # ASR/音频模型(Qwen3-ASR 等):暴露 /v1/audio/transcriptions。每条 prompt 限 1 个
            # 音频项,压低 encoder profiling 显存峰值(PR-0 spike:不限会在小卡 profiling OOM)。
            # --enforce-eager:跳 torch.compile/cudagraph,起得稳(spike 验证的配置)。
            cmd += ["--limit-mm-per-prompt", '{"audio":1}', "--enforce-eager"]
            self.is_multimodal = True
            self.is_audio = True
            logger.info('Detected audio/ASR model — enabling --limit-mm-per-prompt {"audio":1} + --enforce-eager')
        elif auto.get("is_multimodal"):
            # vLLM >=0.6 parses this value with json.loads — must be JSON, not key=val.
            # Allow up to 4 images per prompt by default.
            cmd += ["--limit-mm-per-prompt", '{"image":4}']
            self.is_multimodal = True
            logger.info('Detected multimodal model — enabling --limit-mm-per-prompt {"image":4}')
        else:
            self.is_multimodal = False

        # Tool / function calling. The OpenAI-compat layer injects a Skill tool
        # into every agent chat (see api/routes/openai_compat.py), so requests
        # reach vLLM carrying a ``tools`` payload with ``tool_choice: auto``.
        # vLLM only honours that when launched with --enable-auto-tool-choice
        # and a matching --tool-call-parser; without them it rejects the whole
        # request with 400 (silently tolerated by older vLLM, enforced since
        # the 0.22 line — the cause of every agent chat 400'ing after the
        # upgrade). ``qwen3_xml`` is the parser for Qwen3.6 (it emits XML
        # tool calls: <tool_call><function=NAME><parameter=..>..</parameter>
        # </function></tool_call>) — NOT ``hermes`` (that expects JSON inside
        # <tool_call> and leaves Qwen3.6's XML as raw text, tool_calls=[]).
        # Only generative LLMs take these flags: a pooling/embedding server or
        # an audio/ASR server does not accept them and would fail to launch,
        # so gate them out.
        _is_pooling = self._vllm_runner == "pooling"
        if not auto.get("is_audio") and not _is_pooling:
            cmd += ["--enable-auto-tool-choice", "--tool-call-parser", "qwen3_xml"]
            logger.info("Enabling tool calling — --enable-auto-tool-choice --tool-call-parser qwen3_xml")

        # yaml 的 `params.vllm_args` 最后并进来:同名 flag 以它为准(把适配器那份摘掉),
        # 其余追加。放在 apply_visible_devices 之前,好让启动日志打的是最终 argv。
        # 落卡不受影响 —— --model/--port/--device 在 __init__ 的 render 阶段就被拒了。
        # `{model_dir}` 在这里才展开:构造期还不知道 model_path 最终解析成哪个绝对路径。
        cmd = merge_vllm_args(
            cmd, substitute_model_dir(self._vllm_extra_argv, model_path), label="vLLM",
        )

        # Set cache directories to persistent storage (avoid re-compilation)
        env = dict(os.environ)
        from src.config import get_settings
        _cache_root = str(Path(get_settings().LOCAL_MODELS_PATH) / ".cache")
        env["TORCH_HOME"] = str(Path(_cache_root) / "torch")
        env["XDG_CACHE_HOME"] = _cache_root

        # torch 2.11 / CUDA 13(Blackwell sm_120):flashinfer 的 sampler JIT kernel 要 nvcc
        # (cuda-toolkit-13-0)才能现编;没 nvcc 时 vLLM EngineCore 初始化直接失败。回退到
        # TORCH_SDPA attention + 关 flashinfer sampler(spike 2f452cf 真机验证 vllm 0.22 可起)。
        # setdefault:装了 nvcc / 想用 flashinfer 的,在 .env 覆盖这俩即可。
        env.setdefault("VLLM_ATTENTION_BACKEND", "TORCH_SDPA")
        env.setdefault("VLLM_USE_FLASHINFER_SAMPLER", "0")

        # 落卡:**永远显式钉 CUDA_VISIBLE_DEVICES**(单卡也钉)。见 _placement 的三条不变式。
        apply_visible_devices(
            env,
            LaunchPlacement(cards=cards, tp=tp,
                            total_gb=auto.get("gpu_total_gb", 0.0),
                            free_gb=auto.get("gpu_free_gb", 0.0)),
            cmd, "vLLM",
        )

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,  # Create process group for clean kill
        )
        self._managed = True
        # 启动后台抽干线程(daemon),持续读 stdout 进 deque 防 PIPE 填满死锁。
        # 进程退出 → stdout EOF → 线程自然结束。
        self._stdout_tail.clear()
        self._drain_thread = threading.Thread(
            target=self._drain_stdout, name=f"vllm-stdout-{self._port}", daemon=True
        )
        self._drain_thread.start()

        # Wait for vLLM to become healthy (up to 10 minutes for first-time CUDA kernel compilation)
        start = time.monotonic()
        timeout = 600
        last_log = 0
        try:
            while time.monotonic() - start < timeout:
                if self._process.poll() is not None:
                    # Process exited — 等抽干线程收尾后取尾部日志做诊断(不再直接 read,
                    # stdout 已被 drain 线程消费)。
                    if self._drain_thread is not None:
                        self._drain_thread.join(timeout=1.0)
                    output = "".join(self._stdout_tail)
                    logger.error("vLLM process exited with code %d", self._process.returncode)
                    logger.error("vLLM output (last 500 chars): %s", output[-500:])
                    self._kill_process()
                    raise RuntimeError(f"vLLM failed to start: {output[-200:]}")

                if await self._health_check():
                    elapsed = int(time.monotonic() - start)
                    self._model = True
                    logger.info("vLLM ready in %ds at %s", elapsed, self._base_url)
                    return

                # Log progress every 30 seconds
                elapsed_now = int(time.monotonic() - start)
                if elapsed_now - last_log >= 30:
                    last_log = elapsed_now
                    logger.info("vLLM still starting... (%ds elapsed, timeout %ds)", elapsed_now, timeout)

                await asyncio.sleep(5)

            # Timeout
            logger.error("vLLM did not become healthy within %ds", timeout)
            self._kill_process()
            raise RuntimeError(f"vLLM did not become healthy within {timeout}s")
        except Exception:
            # Ensure cleanup on ANY failure
            self._kill_process()
            raise

    def unload(self) -> None:
        """Kill vLLM subprocess and release GPU memory."""
        if self._managed and (self._process is not None or self._adopted_pid is not None):
            logger.info("Unloading vLLM model: killing process (port %s)", self._port)
            self._kill_process()
            logger.info("vLLM process killed, GPU memory released")
        else:
            logger.info("Disconnecting from external vLLM at %s", self._base_url)
        self._model = None
        self._close_client()

    def _close_client(self) -> None:
        """关闭 httpx client 的连接池。审查发现:每次 load 新建 adapter(含新 _client),
        unload 丢弃却从不 aclose → 每轮 load/unload 泄漏一个 AsyncClient(连接池 + FD)。

        unload 是 sync 且可能在 to_thread 工作线程里跑(无 running loop),故两条路径都兜:
        有正在跑的 loop → schedule aclose(fire-and-forget);无 loop → asyncio.run 同步关。
        """
        client = getattr(self, "_client", None)
        if client is None:
            return
        self._client = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        try:
            if loop is not None and loop.is_running():
                loop.create_task(client.aclose())
            else:
                asyncio.run(client.aclose())
        except Exception:  # noqa: BLE001 — 关闭 best-effort,别挡住 unload
            pass

    def _drain_stdout(self) -> None:
        """后台线程:持续读子进程 stdout 进有界 deque,防 PIPE 填满阻塞子进程。

        阻塞迭代到 stdout EOF(进程退出时关闭)→ 线程自然结束。读异常静默吞
        (进程被 kill 时 stdout 可能突然关闭)。
        """
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self._stdout_tail.append(line)
        except Exception:  # noqa: BLE001 — 抽干线程任何异常都不该冒泡
            pass

    def _kill_process(self) -> None:
        import signal
        from src.services.safe_signal import safe_killpg

        # Kill subprocess we spawned. safe_killpg refuses pgid<=1 (broadcast guard)
        # — even our own child can resolve to a bad pgid if it died + PID recycled.
        if self._process is not None:
            try:
                if safe_killpg(self._process.pid, signal.SIGTERM):
                    try:
                        self._process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        safe_killpg(self._process.pid, signal.SIGKILL)
                        self._process.wait(timeout=5)
            except Exception as e:
                logger.warning("Error killing vLLM subprocess: %s", e)
            finally:
                # 显式关 stdout 管道:光把 Popen 丢掉,读端 FD 要等 GC 才回收
                # (-W default 下就是 "ResourceWarning: unclosed file")。只在进程确已
                # 退出、且抽干线程已收尾时关 —— 进程还活着时 _drain_stdout 正阻塞在
                # 这个 fd 上,从别的线程抽它是自找麻烦;那种情况留给 GC。
                if self._process.poll() is not None:
                    if self._drain_thread is not None:
                        self._drain_thread.join(timeout=1.0)
                    if self._process.stdout is not None:
                        with contextlib.suppress(Exception):
                            self._process.stdout.close()
                self._process = None
                self._drain_thread = None
            return

        # Kill adopted orphan process. Re-validate identity before signalling:
        # the adopted PID came from an external scan and may have died (GPU fell
        # off the bus) and been recycled to an unrelated process — killpg on a
        # stale PID is friendly fire (has taken down mihomo / sshd / user sessions).
        # safe_killpg enforces both the cmdline verify and the pgid<=1 broadcast guard.
        adopted = getattr(self, "_adopted_pid", None)
        if adopted:
            try:
                if safe_killpg(adopted, signal.SIGTERM, verify=_pid_cmdline_is_vllm):
                    logger.info("Sent SIGTERM to adopted vLLM process group (pid=%d)", adopted)
                else:
                    logger.warning(
                        "Refused to kill adopted PID %d (recycled PID or broadcast guard).",
                        adopted,
                    )
            except Exception as e:
                logger.warning("Error killing adopted vLLM (pid=%d): %s", adopted, e)
            finally:
                self._adopted_pid = None

    @property
    def pid(self) -> int | None:
        """Return the PID of the managed vLLM process, if any."""
        if self._process is not None:
            return self._process.pid
        return self._adopted_pid

    async def _health_check(self) -> bool:
        try:
            resp = await self._client.get(f"{self._base_url}/v1/models", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    async def _fetch_remote_max_model_len(self) -> int | None:
        """从运行中 vLLM 的 /v1/models 读 max_model_len(model card 暴露)。重连/adopt 时
        yaml 没配 _max_model_len 的兜底来源。失败/字段缺 → None(再退 4096)。"""
        try:
            resp = await self._client.get(f"{self._base_url}/v1/models", timeout=3)
            if resp.status_code != 200:
                return None
            for m in (resp.json().get("data") or []):
                v = m.get("max_model_len")
                if isinstance(v, int) and v > 0:
                    return v
        except Exception:  # noqa: BLE001 — best-effort
            return None
        return None

    def _validate_base_url(self) -> None:
        """vLLM only on localhost (defense-in-depth — admin-controlled config)."""
        parsed = urllib.parse.urlparse(self._base_url or "")
        if parsed.hostname and parsed.hostname not in ALLOWED_LLM_HOSTS:
            raise ValueError(f"vLLM base_url 只允许 localhost，收到: {parsed.hostname}")

    def _clamp_max_tokens(self, requested: int) -> int:
        """Per-model max_model_len enforcement (replaces TextRequest schema ceiling).

        Outside-voice #7a: 200k-context models must not be rejected at schema layer.
        """
        model_max = getattr(self, "max_model_len", None) or 4096
        safe_max = max(model_max - 512, model_max // 2)
        return min(requested, safe_max)

    def _build_payload(self, req: TextRequest) -> dict[str, Any]:
        return {
            "model": req.model,
            "messages": [m.model_dump(mode="json") for m in req.messages],
            "temperature": req.temperature,
            "max_tokens": self._clamp_max_tokens(req.max_tokens),
            # Always pass explicit value — Qwen3's chat template defaults to
            # thinking=True; omitting the flag still produces reasoning traces.
            "chat_template_kwargs": {"enable_thinking": req.enable_thinking},
            **req.extra,
        }

    def _build_headers(self, req: TextRequest) -> dict[str, str]:
        if req.api_key:
            return {"Authorization": f"Bearer {req.api_key}"}
        return {}

    async def infer(self, req: InferenceRequest) -> InferenceResult:
        """Non-streaming chat completion. Wraps vLLM /v1/chat/completions."""
        if not isinstance(req, TextRequest):
            raise TypeError(f"VLLMAdapter expects TextRequest, got {type(req).__name__}")
        self._validate_base_url()

        t0 = time.monotonic()
        resp = await self._client.post(
            f"{self._base_url}/v1/chat/completions",
            json=self._build_payload(req),
            headers=self._build_headers(req),
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", {}).get("message", resp.text[:300])
            except Exception:
                detail = resp.text[:300]
            raise RuntimeError(f"vLLM API error ({resp.status_code}): {detail}")

        body = resp.json()
        # round9:vLLM 偶发 200-但-body-级-error(OpenAI 错误体 {"object":"error",...}),
        # 只判 status_code 会当成功 → 下游 llm.py 拿不到 choices、静默吐空回复。显式检查。
        if isinstance(body, dict) and (body.get("object") == "error" or body.get("error")):
            err = body.get("message") or body.get("error") or "unknown error"
            if isinstance(err, dict):
                err = err.get("message") or str(err)
            raise RuntimeError(f"vLLM API error (200 body): {err}")
        usage_dict = body.get("usage") or {}
        usage = UsageMeter(
            input_tokens=usage_dict.get("prompt_tokens"),
            output_tokens=usage_dict.get("completion_tokens"),
            latency_ms=latency_ms,
        )
        return InferenceResult(
            media_type="application/json",
            data=resp.content,
            metadata={"raw": body},
            usage=usage,
        )

    async def infer_stream(self, req: InferenceRequest) -> AsyncIterator[StreamEvent]:
        """SSE stream → yields StreamEvent('delta', {chunk}) / ('done', {usage})."""
        if not isinstance(req, TextRequest):
            raise TypeError(f"VLLMAdapter expects TextRequest, got {type(req).__name__}")
        self._validate_base_url()

        payload = self._build_payload(req)
        payload["stream"] = True
        # round9:_build_payload 展开 **req.extra,调用方在 extra 里塞 stream_options
        # 会盖掉这里 —— setdefault 又不会纠正,导致 include_usage 缺失 → 服务端不发 usage
        # chunk → 计费拿空。强制合并 include_usage=True,保留调用方其它 stream_options 键。
        _so = dict(payload.get("stream_options") or {})
        _so["include_usage"] = True
        payload["stream_options"] = _so

        last_usage: dict[str, Any] | None = None
        async with self._client.stream(
            "POST",
            f"{self._base_url}/v1/chat/completions",
            json=payload,
            headers=self._build_headers(req),
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                yield StreamEvent(
                    type="error",
                    payload={"status_code": resp.status_code, "body": body[:300].decode("utf-8", errors="replace")},
                )
                return
            try:
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload_text = line[5:].strip()
                    if payload_text == "[DONE]":
                        break
                    try:
                        chunk = _json.loads(payload_text)
                    except _json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        last_usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    delta = choices[0].get("delta") if choices else None
                    if delta:
                        yield StreamEvent(type="delta", payload=delta)
            except httpx.HTTPError as e:
                # 中途断流(vLLM 崩溃/网络 reset/读超时)—— yield 结构化 error 事件(带已产
                # usage 供计费结算),与首段 status!=200 的错误契约一致,不让 httpx 异常裸
                # 冒泡拖垮上游 SSE 流。
                yield StreamEvent(
                    type="error",
                    payload={
                        "error": f"stream interrupted: {type(e).__name__}: {e}",
                        "usage": last_usage or {},
                    },
                )
                return
        yield StreamEvent(type="done", payload={"usage": last_usage or {}})
