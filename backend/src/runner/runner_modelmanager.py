"""runner 子进程内的 ModelManager 工厂（spec §4.5）。

spec §4.5：每个 TTS runner 子进程持有**自己的** ModelManager 实例 —— 不是
主进程的 app.state.model_manager（子进程根本拿不到那个对象）。本模块把「子进程内
构造 ModelRegistry + GPUAllocator + ModelManager」收进一个工厂函数，单独可测。

fake_adapter=True 时（Lane C/D 测试、无真模型的环境）：注入一个 adapter_factory,
让 ModelManager.load_model 对**所有** spec 都实例化 FakeAdapter —— 这样 runner
框架（IPC + 生命周期 + per-model 锁）能在零 GPU 下跑通。真实部署 fake_adapter=
False，adapter 由 ModelSpec.adapter_class 决定（registry 从 yaml 读）。
"""
from __future__ import annotations

import logging
import os

from src.runner.fake_adapter import FakeAdapter
from src.services.gpu_allocator import GPUAllocator
from src.services.inference.base import InferenceAdapter
from src.services.inference.registry import ModelRegistry, ModelSpec
from src.services.model_manager import ModelManager

logger = logging.getLogger(__name__)

# 真实部署的 models.yaml 默认位置 —— 环境变量优先。
_DEFAULT_MODELS_YAML = os.getenv("NOUS_MODELS_YAML", "config/models.yaml")


def _fake_adapter_factory(spec: ModelSpec) -> InferenceAdapter:
    """所有 spec 都走 FakeAdapter —— spec.params 里的 fail_load / infer_seconds /
    oom_on_load_count 透传给构造，这样测试能通过 yaml 配置 runner 的故障行为。"""
    return FakeAdapter(paths=spec.paths, **dict(spec.params))


def build_runner_model_manager(
    group_id: str,
    gpus: list[int],
    *,
    models_yaml_path: str | None = None,
    fake_adapter: bool = False,
) -> ModelManager:
    """在 runner 子进程内构造一个独立的 ModelManager。

    Parameters
    ----------
    group_id / gpus:
        本 runner 负责的 GPU group。gpus 作为 GPUAllocator 的 preferred_gpus —— device=auto
        解析优先落在本 runner 的卡里(装不下才 spill,见 get_best_gpu),守住 per-group 隔离。
    models_yaml_path:
        models.yaml 路径。None → NOUS_MODELS_YAML 环境变量 → config/models.yaml。
    fake_adapter:
        True → 所有 spec 走 FakeAdapter（测试 / 无真模型）。
    """
    yaml_path = models_yaml_path or _DEFAULT_MODELS_YAML
    registry = ModelRegistry(yaml_path)
    # gpus = 本 runner group 的卡 → preferred_gpus,让 device=auto 优先落本 group
    # (装不下才 spill),修「runner 分配 gpu0 却把模型装到全局最空的 gpu1」隔离漏洞。
    allocator = GPUAllocator(preferred_gpus=gpus or None)
    mm = ModelManager(registry=registry, allocator=allocator)
    logger.info(
        "runner ModelManager built: group=%s gpus=%s yaml=%s fake=%s "
        "(%d specs)",
        group_id, gpus, yaml_path, fake_adapter, len(registry.specs),
    )

    if fake_adapter:
        # fake 模式：把 load_model / get_or_load 的 adapter_factory 默认值绑成
        # FakeAdapter 工厂。用 functools.wraps 包原方法 —— 不改 ModelManager
        # 类（其它真实路径不受影响）。
        import functools

        orig_load = mm.load_model
        orig_get_or_load = mm.get_or_load

        @functools.wraps(orig_load)
        async def _load_model(model_id, adapter_factory=None):
            return await orig_load(
                model_id, adapter_factory=adapter_factory or _fake_adapter_factory,
            )

        @functools.wraps(orig_get_or_load)
        async def _get_or_load(model_id, adapter_factory=None):
            return await orig_get_or_load(
                model_id, adapter_factory=adapter_factory or _fake_adapter_factory,
            )

        mm.load_model = _load_model       # type: ignore[method-assign]
        mm.get_or_load = _get_or_load     # type: ignore[method-assign]

    return mm
