from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class ModelSpec(BaseModel):
    """Static model registry entry (yaml-loaded or scanner-synthesized).

    Frozen — instances are stored in registry dict + adapter instantiation
    closures, must be hashable / immutable.
    """

    id: str
    model_type: str
    adapter_class: str
    paths: dict[str, str]  # multi-component paths; single-component uses {"main": "..."}
    vram_mb: int
    params: dict[str, Any] = Field(default_factory=dict)
    resident: bool = False
    ttl_seconds: int = 300
    gpu: int | list[int] | None = None
    # GPU 组(张量并行):`[0, 2]` = 把这组卡当一个单元用,tp=len(gpus)。**优先于 `gpu`**。
    # 来源:models.yaml 的 `gpus:` 或运行时覆盖 model_runtime_overrides.gpus。
    # None = 未配 → 单卡 `gpu` / 自动选卡。
    gpus: list[int] | None = None
    preload_order: int | None = None

    model_config = ConfigDict(frozen=True)


def _ov_gpus(ov: dict, entry: dict):
    """运行时覆盖的 gpus 优先于 YAML 的 —— 覆盖里的 `[]` 是「显式清空组」,
    要盖住 YAML 的 `gpus:`(不是"没设"→ 回退 YAML)。见 model_runtime_override.gpus 三态。"""
    return ov["gpus"] if "gpus" in ov else entry.get("gpus")


def _log_override_mismatch(model_id: str, yaml_entry: dict, ov: dict) -> None:
    """覆盖表(model_runtime_overrides)盖掉 yaml 的常驻/落卡声明时留痕。

    2026-09-05:`qwen3_embedding_8b` 的 yaml 写了 `resident: true`,DB 覆盖行却是
    `resident=False, gpu=2` —— 目录里改 yaml 看着已生效,机器上常驻根本没起来,
    且**一行日志都没有**。覆盖优先本身是设计(UI 的 PATCH 要能压住目录),
    「悄悄地」才是问题:目录承诺与实际不一致必须能从启动日志一眼看出来。
    常驻不生效直接等于显存规划失真 → error;落卡换卡是可以接受的运维动作 → warning。
    """
    if yaml_entry.get("resident") is True and ov.get("resident") is False:
        logger.error(
            "模型 %s yaml 声明 resident:true 但 model_runtime_overrides 覆盖为 False"
            " —— 目录承诺与实际不一致,常驻不会生效",
            model_id,
        )
    y_gpus = yaml_entry.get("gpus")
    if "gpus" in ov and y_gpus and list(ov["gpus"] or []) != list(y_gpus):
        logger.warning(
            "模型 %s yaml 声明 gpus=%s 但 model_runtime_overrides 覆盖为 %s —— 实际落卡以覆盖为准",
            model_id, y_gpus, ov["gpus"],
        )
    y_gpu = yaml_entry.get("gpu")
    if "gpu" in ov and y_gpu is not None and ov["gpu"] != y_gpu:
        logger.warning(
            "模型 %s yaml 声明 gpu=%s 但 model_runtime_overrides 覆盖为 %s —— 实际落卡以覆盖为准",
            model_id, y_gpu, ov["gpu"],
        )


def _coerce_paths(entry: dict[str, Any]) -> dict[str, str]:
    """Read `paths` block from a yaml entry; legacy `path` falls into paths['main']."""
    paths = entry.get("paths") or {}
    if not paths and entry.get("path"):
        paths = {"main": entry["path"]}
    return paths


class ModelRegistry:
    def __init__(self, config_path: str):
        self._config_path = config_path
        self._specs: dict[str, ModelSpec] = {}
        self._load(config_path)

    def reload(self) -> int:
        """Hot-reload config from disk. Returns number of new specs added."""
        old_ids = set(self._specs.keys())
        self._load(self._config_path)
        new_ids = set(self._specs.keys()) - old_ids
        if new_ids:
            logger.info("Registry reloaded, new models: %s", new_ids)
        return len(new_ids)

    def _load(self, config_path: str) -> None:
        # 模型定义走 collect_model_entries(models.d/*.yaml + models.yaml,单一来源;
        # spec 2026-06-20 一模型一文件)—— 与 load_model_configs 同一入口,口径不分叉。
        # 运行时覆盖单一来源(spec 2026-06-16「数据加载统一」):registry 历史上直读 yaml,
        # 绕过 runtime_overrides.json overlay → overlay 的 gpu/resident 对 vLLM 落卡不生效
        # (落卡读 spec.gpu)。这里套用 overlay,使 overlay 成为运行时覆盖的唯一来源。
        # 局部 import 避免 registry↔config 启动期循环(同 add_from_scan)。
        from pathlib import Path as _Path  # noqa: PLC0415
        from src.config import collect_model_entries, load_runtime_overrides  # noqa: PLC0415
        from src.gpu.topology import sanitize_config_gpus  # noqa: PLC0415
        overrides = load_runtime_overrides()
        for entry in collect_model_entries(_Path(config_path)):
            ov = overrides.get(entry["id"]) or {}
            _log_override_mismatch(entry["id"], entry, ov)
            spec = ModelSpec(
                id=entry["id"],
                model_type=entry["type"],
                adapter_class=entry["adapter"],
                paths=_coerce_paths(entry),
                vram_mb=entry.get("vram_mb", 0),
                params=entry.get("params", {}),
                resident=ov.get("resident", entry.get("resident", False)),
                ttl_seconds=entry.get("ttl_seconds", 3600 if entry["type"] == "llm" else 300),
                gpu=ov.get("gpu", entry.get("gpu")),
                # YAML / 覆盖里的 gpus 过一遍校验(大小、去重、存在、同型号)——
                # 非法就 log error 并忽略该字段(按单卡走),绝不流到适配器(审查 #14)。
                # ov 里的 [] 是"显式清空组"的哨兵:sanitize 返回 None,回落单卡 gpu。
                gpus=sanitize_config_gpus(entry["id"], {"gpus": _ov_gpus(ov, entry)}),
                preload_order=entry.get("preload_order"),
            )
            self._specs[spec.id] = spec
            logger.debug("Loaded model spec: %s (%s)", spec.id, spec.model_type)

    def set_resident(self, model_id: str, resident: bool) -> bool:
        """翻内存 spec 的常驻位。ModelSpec 是 frozen → model_copy 换新对象。

        PATCH /engines/{name}/resident 持久化走 DB override(重启后 _load 叠加),
        这里补运行期同步 —— 否则 /health 的 resident 统计和 preload_residents
        名单读的是启动时快照,与 UI 显示的 override 脱节直到重启。
        """
        spec = self._specs.get(model_id)
        if spec is None:
            return False
        self._specs[model_id] = spec.model_copy(update={"resident": resident})
        return True

    @property
    def specs(self) -> list[ModelSpec]:
        return list(self._specs.values())

    def get(self, model_id: str) -> ModelSpec | None:
        return self._specs.get(model_id)

    def list_by_type(self, model_type: str) -> list[ModelSpec]:
        return [s for s in self._specs.values() if s.model_type == model_type]

    def add_from_scan(self, model_id: str) -> ModelSpec | None:
        """Synthesize a ModelSpec from the on-disk scanner output.

        Used as a load-time fallback when `model_id` wasn't in the model
        configs (`configs/models.d/*.yaml` + legacy models.yaml) at startup.
        Auto-detected LLM/VL entries fill `adapter` (always
        VLLMAdapter); image/video do not — those return None here so the
        caller raises ValueError instead of silently registering an
        unloadable spec.

        Imported locally to avoid a startup-time circular import between
        registry and model_scanner.
        """
        from src.services.model_scanner import scan_models

        configs = scan_models()
        cfg = configs.get(model_id)
        if cfg is None:
            return None
        adapter = cfg.get("adapter")
        if not adapter:
            return None
        # Scanner emits `local_path` for single-component models; image/video
        # emit `paths` dict directly when known.
        paths = cfg.get("paths") or {}
        if not paths and cfg.get("local_path"):
            paths = {"main": cfg["local_path"]}
        # 覆盖仍在这里显式套一遍 —— scan_models() 自己也叠(2026-09-03 分层修复后),
        # 但这里要的是 `gpus` 的三态语义(_ov_gpus:NULL=未覆盖 / []=显式清空组),
        # 跟 _load 同一套。两处叠加同源同键,结果一致,不会互相打架。
        from src.config import load_runtime_overrides  # noqa: PLC0415
        from src.gpu.topology import sanitize_config_gpus  # noqa: PLC0415
        ov = load_runtime_overrides().get(model_id) or {}
        # 不一致告警要拿 **yaml 原样** 比:`cfg` 出自 scan_models(),覆盖在它返回前
        # 已经叠过一遍,拿它比等于自己跟自己比,永远不报(2026-09-05)。
        from src.config import load_model_configs  # noqa: PLC0415
        _yaml_entry = load_model_configs(apply_overrides=False).get(model_id) or {}
        _log_override_mismatch(model_id, _yaml_entry, ov)
        spec = ModelSpec(
            id=model_id,
            model_type=cfg.get("type", "llm"),
            adapter_class=adapter,
            paths=paths,
            vram_mb=int(round(cfg.get("vram_gb", 0) * 1024)),
            params=cfg.get("params", {}),
            resident=ov.get("resident", cfg.get("resident", False)),
            ttl_seconds=cfg.get("ttl_seconds", 3600 if cfg.get("type") == "llm" else 300),
            gpu=ov.get("gpu", cfg.get("gpu")),
            gpus=sanitize_config_gpus(model_id, {"gpus": _ov_gpus(ov, cfg)}),
            preload_order=cfg.get("preload_order"),
        )
        self._specs[spec.id] = spec
        logger.info(
            "Auto-registered model spec from scan: %s (%s, %s)",
            spec.id, spec.model_type, spec.adapter_class,
        )
        return spec
