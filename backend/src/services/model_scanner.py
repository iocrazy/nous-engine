"""Auto-scan models directory to detect model type and config."""
import json
import logging
import time
from pathlib import Path
from typing import Any

from src.config import _apply_runtime_overrides, get_settings, load_model_configs

logger = logging.getLogger(__name__)

# 进程内 TTL 缓存,和 lora_scanner 对齐。scan_models 会 iterdir 走盘 + 每候选目录
# open()+json.load + _estimate_vram rglob;/api/v1/engines 与 /api/v1/models 两个独立
# @cached prefix 各 miss 各扫一遍(每 30s 最多 2 次全扫)。这层模块级缓存让实际走盘频率
# 与响应缓存解耦。/scan 端点或重启会 invalidate。性能 P1-4。
#
# **缓存的是「磁盘 + yaml」的基础结果,不含运行时覆盖**(resident/gpu/gpus/vram_budget)。
# 覆盖在 scan_models 返回前每次现叠(runtime_override_store 是进程内 write-through 缓存,
# 零 I/O)。曾经把两者一起缓存 → PATCH /engines/{name}/resident 写完 DB + invalidate("engines")
# 后,重算的列表 body 仍是 30s 前的旧覆盖值,字节一样 → ETag 一样 → 浏览器拿 304,
# 常驻徽标不变,用户以为没生效(2026-09-03 线上复现)。
_SCAN_CACHE: dict = {"data": None, "ts": 0.0, "base": None}
_SCAN_TTL_SECONDS = 30


def invalidate_scan_cache() -> None:
    """清模型扫描缓存 —— /scan 端点或模型目录变更后调用,强制下次重扫。"""
    _SCAN_CACHE["data"] = None
    _SCAN_CACHE["ts"] = 0.0
    _SCAN_CACHE["base"] = None


# `diffusers/` contains complete model directories. Component buckets contain
# individual weights and are enumerated by component-node executors.
_MEDIA_MODEL_SUBDIRS = {"diffusers"}


def _iter_candidate_model_dirs(type_dir: Path):
    """Yield (model_dir, local_path) pairs for one type/ tree.

    LLM/TTS/VL/embedding stay depth-2 (`<type>/<model>`). Complete media models
    are depth-3 under `diffusers/`. Component buckets are skipped because their
    individual weights are listed by component-node executors.

    2026-09-11:embedding 从 `text/embedding/<model>` 提成顶层桶 `embedding/<model>`
    (`text/` 下只有这一个 bucket,那层纯属多余),depth-3 特例随之删除。
    """
    if type_dir.name == "media":
        for sub in sorted(type_dir.iterdir()):
            if not sub.is_dir() or sub.name not in _MEDIA_MODEL_SUBDIRS:
                continue
            for model_dir in sorted(sub.iterdir()):
                if model_dir.is_dir():
                    yield model_dir, f"{type_dir.name}/{sub.name}/{model_dir.name}"
        return
    for model_dir in sorted(type_dir.iterdir()):
        if model_dir.is_dir():
            yield model_dir, f"{type_dir.name}/{model_dir.name}"


def scan_models() -> dict[str, dict[str, Any]]:
    """Scan LOCAL_MODELS_PATH and merge with models.yaml configs (TTL-cached).

    Auto-detects:
    - LLM: has config.json with "model_type" field
    - Image (diffusers): has model_index.json (under `media/diffusers/<X>/`)
    - TTS: matched by models.yaml only (no auto-detect)

    Returns merged dict: models.yaml configs + auto-detected models,运行时覆盖已叠加。
    """
    # cache 按 models 根路径归属:路径变了(测试 tmp_path、搬盘)直接算 miss,避免返回
    # 上一路径的陈旧结果。
    base = str(get_settings().LOCAL_MODELS_PATH)
    now = time.time()
    cached = _SCAN_CACHE["data"]
    if (
        cached is not None
        and _SCAN_CACHE["base"] == base
        and now - _SCAN_CACHE["ts"] < _SCAN_TTL_SECONDS
    ):
        return _with_runtime_overrides(cached)
    result = _scan_models_uncached()
    _SCAN_CACHE["data"] = result
    _SCAN_CACHE["ts"] = now
    _SCAN_CACHE["base"] = base
    return _with_runtime_overrides(result)


def _with_runtime_overrides(scanned: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """基础扫描结果 + 当前运行时覆盖 → 新 dict(**不改 base**,缓存里永远是干净基础值)。

    覆盖来自 runtime_override_store 的进程内 write-through 缓存,所以每次读都拿得到
    最新值,不用指望每条写路径记得清扫描缓存。被覆盖到的条目做浅拷贝,免得叠加值
    渗进 _SCAN_CACHE(渗进去 = 覆盖被 30s 地烘死,正是原 bug)。
    """
    merged = dict(scanned)
    _apply_runtime_overrides(merged, copy_before_write=True)
    return merged


def _scan_models_uncached() -> dict[str, dict[str, Any]]:
    settings = get_settings()
    base = Path(settings.LOCAL_MODELS_PATH)
    # apply_overrides=False:覆盖由 scan_models 在**每次返回前**叠,绝不烘进 TTL 缓存。
    yaml_configs = load_model_configs(apply_overrides=False)

    # Start with yaml configs
    result = dict(yaml_configs)

    if not base.exists():
        return result

    for type_dir in sorted(base.iterdir()):
        if not type_dir.is_dir():
            continue
        for model_dir, local_path in _iter_candidate_model_dirs(type_dir):
            yaml_key = _find_yaml_key(yaml_configs, local_path)
            if yaml_key:
                continue
            detected = _detect_model(model_dir, local_path)
            if detected:
                key = _make_key(model_dir.name)
                result[key] = detected
                logger.info(
                    "Auto-detected model: %s (%s) at %s",
                    key, detected["type"], local_path,
                )

    return result


def _find_yaml_key(configs: dict, local_path: str) -> str | None:
    """Find yaml config key that matches this local_path."""
    for key, cfg in configs.items():
        if cfg.get("local_path") == local_path:
            return key
    return None


def _make_key(dir_name: str) -> str:
    """Convert directory name to a config key."""
    return dir_name.lower().replace("-", "_").replace(".", "_")


_VLLM_ADAPTER = "src.services.inference.llm_vllm.VLLMAdapter"


def _detect_model(model_dir: Path, local_path: str) -> dict[str, Any] | None:
    """Auto-detect model type from directory contents.

    LLM and VL detections fill `adapter` so the registry can synthesize a
    ModelSpec on demand and the load button actually works. Image/video
    intentionally do NOT fill `adapter` — there's no diffusers adapter
    implemented yet, so leaving it blank lets the UI render a "未注册"
    badge and disable the load button instead of letting the user click
    into a confusing "Unknown model" failure.
    """

    # 目录带 .nous-external 标记 = 由外部微服务托管(如 MOSS-Transcribe-Diarize 走
    # nous-moss-asr systemd,spec 2026-07-20-moss-asr-sglang-serving)。不生成引擎卡:
    # ModelManager 不认这些架构,点加载必报 Unknown engine,卡片纯误导。
    if (model_dir / ".nous-external").exists():
        return None

    # Check for HuggingFace LLM (config.json with model_type)
    config_json = model_dir / "config.json"
    if config_json.exists():
        try:
            with open(config_json) as f:
                cfg = json.load(f)
            model_type = cfg.get("model_type", "")
            if model_type:
                # It's an LLM or VL model
                architectures = cfg.get("architectures", [])
                is_vl = any(
                    "VL" in a or "Vision" in a or "visual" in a.lower()
                    for a in architectures
                )

                return {
                    "name": model_dir.name,
                    "type": "understand" if is_vl else "llm",
                    "engine": "vllm",
                    "adapter": _VLLM_ADAPTER,
                    "gpu": 0,
                    "vram_gb": _estimate_vram(model_dir),
                    "resident": False,
                    "local_path": local_path,
                    "auto_detected": True,
                }
        except (json.JSONDecodeError, OSError):
            pass

    # Check for diffusers model (model_index.json)
    model_index = model_dir / "model_index.json"
    if model_index.exists():
        try:
            with open(model_index) as f:
                idx = json.load(f)
            class_name = idx.get("_class_name", "")

            # Determine if image or video
            is_video = "video" in class_name.lower() or "wan" in model_dir.name.lower()

            return {
                "name": model_dir.name,
                "type": "video" if is_video else "image",
                # NB: no `adapter` — diffusers adapter is unimplemented.
                "gpu": 0,
                "vram_gb": _estimate_vram(model_dir),
                "resident": False,
                "local_path": local_path,
                "auto_detected": True,
            }
        except (json.JSONDecodeError, OSError):
            pass

    return None


def _estimate_vram(model_dir: Path) -> float:
    """Estimate VRAM from model file sizes (safetensors/bin/pt)."""
    # 单次 rglob 按后缀过滤,替代原来 4 次独立递归遍历(性能 P1-4)。
    exts = {".safetensors", ".bin", ".pt", ".onnx"}
    total = sum(
        f.stat().st_size
        for f in model_dir.rglob("*")
        if f.suffix in exts and f.is_file()
    )

    gb = round(total / (1024**3), 1)
    # VRAM ~ 1.2x model file size (overhead for activations)
    return round(gb * 1.2, 1) if gb > 0 else 0
