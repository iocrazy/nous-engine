"""GPU Runner IPC 协议 —— 主进程 <-> TTS runner 子进程的 wire format.

spec §3.3。走 multiprocessing.Pipe，msgpack 编码（dev 模式 NOUS_IPC_FORMAT=json
fallback 便于 journalctl 调试）。**仅 TTS runner 走此协议**；LLM runner 不
收 RunNode，主进程直连其 vLLM HTTP 端口（Lane E）。

消息是 frozen dataclass —— 跨进程边界传不可变值，避免别名 bug。每个消息有一个
`kind` 字面量做判别式，`decode` 按 kind 路由回正确的 dataclass。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal


class ProtocolError(Exception):
    """编解码失败 / 未知消息 kind。"""


# ------------------------------------------------------------------
# 主进程 -> TTS runner
# ------------------------------------------------------------------


@dataclass(frozen=True)
class LoadModel:
    model_key: str
    config: dict[str, Any] = field(default_factory=dict)
    kind: Literal["load_model"] = "load_model"


@dataclass(frozen=True)
class UnloadModel:
    model_key: str
    kind: Literal["unload_model"] = "unload_model"


@dataclass(frozen=True)
class RunNode:
    task_id: int
    node_id: str
    node_type: str  # 仅 "tts"
    model_key: str | None
    inputs: dict[str, Any]
    is_deterministic: bool = False
    kind: Literal["run_node"] = "run_node"


@dataclass(frozen=True)
class Abort:
    task_id: int
    node_id: str | None = None
    kind: Literal["abort"] = "abort"


@dataclass(frozen=True)
class Ping:
    kind: Literal["ping"] = "ping"


@dataclass(frozen=True)
class SetModelResident:
    """主进程 → runner:切已加载 by-key 模型的常驻位(组件 L1 PR-2c)。
    model_id = runner _models 的键。runner 走 mm.set_model_resident;
    没加载则 no-op;状态经下个 Pong 快照反映。"""
    model_id: str
    resident: bool
    kind: Literal["set_model_resident"] = "set_model_resident"


# ------------------------------------------------------------------
# TTS runner -> 主进程
# ------------------------------------------------------------------


@dataclass(frozen=True)
class Ready:
    """runner 子进程 event loop 起来后发的第一个消息（spec §4.2 生命周期图）。"""

    runner_id: str
    group_id: str
    gpus: list[int]
    kind: Literal["ready"] = "ready"


@dataclass(frozen=True)
class NodeResult:
    task_id: int
    node_id: str
    status: Literal["completed", "failed", "cancelled"]
    outputs: dict[str, Any] | None
    error: str | None
    duration_ms: int
    kind: Literal["node_result"] = "node_result"


@dataclass(frozen=True)
class NodeProgress:
    task_id: int
    node_id: str
    progress: float  # 0.0 ~ 1.0
    detail: str | None = None
    # PR-F:latent → 96px JPEG data URI(ComfyUI Latent2RGB 等价,无 TAESD 权重)。
    # 出图过程中每步可选发一帧,前端节点上叠 thumbnail,「看图慢慢长出来」。
    preview_url: str | None = None
    # PR-1a(2026-05-27 任务面板重置 L3 进度颗粒度):stage + step + per-step latency
    # + ETA。spec §State model TaskProgress。stage 候选:"text_encode" / "dit_denoise" /
    # "vae_decode"(image)/ "tts_synth" / "llm_gen"。全 nullable —— 旧 fake 不发 stage
    # 时这些字段保持 None,前端兜底显示 detail 文本(向后兼容)。
    stage: str | None = None
    step: int | None = None
    total_steps: int | None = None
    step_latency_ms: int | None = None
    eta_ms: int | None = None
    kind: Literal["node_progress"] = "node_progress"


@dataclass(frozen=True)
class ModelEvent:
    event: Literal["loaded", "unloaded", "load_failed"]
    model_key: str
    error: str | None = None
    kind: Literal["model_event"] = "model_event"


@dataclass(frozen=True)
class Pong:
    runner_id: str
    # 结构化已加载 adapter 快照(ModelManager.loaded_models_snapshot()):每条 dict =
    # {model_id, model_type, gpu_index, gpu_indices, vram_mb, pipeline_class,
    #  source_files, last_used_ago_sec}。image/tts adapter 真加载在 runner 自己的
    # _models,主进程靠这份快照(supervisor watchdog 每 ping 一次对账)还原「已加载」
    # 视图。历史上是 list[str](仅 id);改 dict 向后兼容(decode 不校验元素类型)。
    loaded_models: list[dict] = field(default_factory=list)
    # 本 runner 进程的 host RAM stash 池占用快照(MB,spec ram-pinned-linkage PR-1b)。
    # 主进程 /monitor/stats 聚合各 runner + 主进程本体。默认 0 向后兼容。
    stash_ram_mb: int = 0
    kind: Literal["pong"] = "pong"


# kind 字面量 -> dataclass 类的路由表
_KIND_TO_CLASS: dict[str, type] = {
    "load_model": LoadModel,
    "unload_model": UnloadModel,
    "run_node": RunNode,
    "abort": Abort,
    "ping": Ping,
    "set_model_resident": SetModelResident,
    "ready": Ready,
    "node_result": NodeResult,
    "node_progress": NodeProgress,
    "model_event": ModelEvent,
    "pong": Pong,
}

# 类型注解仅供调用方做 isinstance / match —— 任意消息的联合类型
Message = (
    LoadModel | UnloadModel | RunNode | Abort | Ping | SetModelResident
    | Ready | NodeResult | NodeProgress | ModelEvent | Pong
)


def default_format() -> str:
    """wire format：环境变量 NOUS_IPC_FORMAT，默认 msgpack。"""
    fmt = os.getenv("NOUS_IPC_FORMAT", "msgpack").strip().lower()
    return fmt if fmt in ("msgpack", "json") else "msgpack"


def encode(msg: Any, *, fmt: str | None = None) -> bytes:
    """把消息 dataclass 编成 bytes。"""
    fmt = fmt or default_format()
    payload = asdict(msg)
    if fmt == "json":
        return json.dumps(payload).encode("utf-8")
    import msgpack

    return msgpack.packb(payload, use_bin_type=True)


def decode(raw: bytes, *, fmt: str | None = None) -> Any:
    """把 bytes 解回对应的消息 dataclass。未知 kind 抛 ProtocolError。"""
    fmt = fmt or default_format()
    try:
        if fmt == "json":
            payload = json.loads(raw.decode("utf-8"))
        else:
            import msgpack

            payload = msgpack.unpackb(raw, raw=False)
    except Exception as e:  # noqa: BLE001 — 任何解码异常统一包成 ProtocolError
        raise ProtocolError(f"failed to decode {fmt} payload: {e}") from e

    if not isinstance(payload, dict) or "kind" not in payload:
        raise ProtocolError(f"decoded payload is not a tagged message: {payload!r}")
    kind = payload["kind"]
    cls = _KIND_TO_CLASS.get(kind)
    if cls is None:
        raise ProtocolError(f"unknown message kind: {kind!r}")
    # 只取该 dataclass 声明的字段，多余 key 忽略（向前兼容）
    known = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in payload.items() if k in known}
    try:
        return cls(**kwargs)
    except TypeError as e:
        raise ProtocolError(f"payload missing fields for {kind!r}: {e}") from e
