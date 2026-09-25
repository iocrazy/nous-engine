"""服务能力推导:工具调用 / 思考 / 图片输入 / 上下文 / 提供商。

给第三方客户端「供应商」配置抄值用(服务页、API Key 详情页展示,见
`GET /api/v1/services` 的 `capabilities`)。全部**从配置推导**,不探测运行中的引擎:

- 入参 `cfg` 是 `load_model_configs()` 的**生效值**(已叠运行时覆盖),与下次 load
  实际喂给适配器的是同一份。
- 推不出来的一律 None/False,不编。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.services.inference.llm_vllm import is_vision_model_config, normalize_vllm_flag

#: 按 HF config.json 判「吃图」的类型(与 VLLMAdapter 加 --limit-mm-per-prompt 同一判据)。
_CONFIG_JSON_VISION_TYPES = frozenset({"llm", "understand", "vl"})


def _vllm_args(cfg: dict) -> dict[str, Any]:
    """`params.vllm_args`,键统一成 `--flag` 形(下划线/连字符都认)。"""
    raw = (cfg.get("params") or {}).get("vllm_args") or {}
    if not isinstance(raw, dict):
        return {}
    return {normalize_vllm_flag(k): v for k, v in raw.items()}


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _context(cfg: dict, args: dict[str, Any]) -> int | None:
    # vllm_args 与适配器自己拼的同名 flag 冲突时以 vllm_args 为准(merge_vllm_args),这里同口径。
    if "--max-model-len" in args:
        return _as_int(args["--max-model-len"])
    return _as_int((cfg.get("params") or {}).get("max_model_len"))


def _model_dir(cfg: dict) -> Path | None:
    """与 VLLMAdapter 同一套解析:LOCAL_MODELS_PATH / paths.main,不存在再当绝对路径。"""
    from src.config import get_settings  # noqa: PLC0415 — 与 llm_vllm 一样局部取,测试可 monkeypatch

    rel = (cfg.get("paths") or {}).get("main") or cfg.get("local_path")
    if not rel:
        return None
    p = Path(get_settings().LOCAL_MODELS_PATH) / rel
    return p if p.exists() else Path(rel)


def _config_json_vision(cfg: dict) -> bool:
    d = _model_dir(cfg)
    if d is None:
        return False
    try:
        model_config = json.loads((d / "config.json").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(model_config, dict) and is_vision_model_config(model_config)


def _mm_image_limit(args: dict[str, Any]) -> bool:
    limit = args.get("--limit-mm-per-prompt")
    if isinstance(limit, str):
        try:
            limit = json.loads(limit)
        except ValueError:
            return False
    if not isinstance(limit, dict):
        return False
    return (_as_int(limit.get("image")) or 0) > 0


def _vision(cfg: dict, args: dict[str, Any]) -> bool:
    model_type = (cfg.get("type") or "").lower()
    if model_type in _CONFIG_JSON_VISION_TYPES:
        return _config_json_vision(cfg)
    if model_type == "embedding":
        return _mm_image_limit(args)
    return False


def derive_capabilities(cfg: dict) -> dict[str, Any]:
    args = _vllm_args(cfg)
    source = cfg.get("source") or None
    provider = source.split("/", 1)[0] if isinstance(source, str) and "/" in source else None
    return {
        "context": _context(cfg, args),
        # 我们不设单独的输出上限:None = vLLM 默认「上下文 − 输入」。
        "max_output": None,
        "tools": bool(args.get("--enable-auto-tool-choice")),
        "thinking": bool(args.get("--reasoning-parser")),
        "vision": _vision(cfg, args),
        "provider": provider,
        "source": source,
    }


def capabilities_for_service(svc: Any, configs: dict) -> dict[str, Any] | None:
    """服务 → 能力。**两处共用**:管理面 GET /api/v1/services 与数据面 /v1/models。

    只有 source_type=model 的服务有能力可言(工作流 / 图像 / app 返回 None,不编)。
    引擎名 = `source_name or source_id`(_readiness.engine_name_of 同口径);`configs` 应是
    load_model_configs() 的**生效值**(叠了运行时覆盖),于是 context 就是 vLLM 实际按它
    启动的上下文,而不是模型原生上限。
    """
    if getattr(svc, "source_type", None) != "model":
        return None
    cfg = configs.get(svc.source_name or str(svc.source_id))
    return derive_capabilities(cfg) if cfg else None
