"""Display-only enumeration of the registry engines a published service
depends on, for the service overview UI ("有多少模型 + 对应加载情况").

This is **purely static** ref extraction from a frozen workflow snapshot —
NOT model management (it deliberately does not touch the registry /
ModelManager, sidestepping the unified-model-mgmt gap). Live load-state is
overlaid client-side: engines matched by key against /api/v1/engines.

2026-09-26:自建图像引擎删除后不再产出 `kind: "component"` 引用(flux2 组件加载节点
随之消失,出图走 ComfyUI 桥),只剩 `kind: "engine"`。

Snapshot shape (see workflow_publish._build_snapshot):
    {"nodes": {"<id>": {"class_type": <type>, "inputs": <node.data>}}}
Older / editor shape (list of {id, type, data}) is also tolerated.
"""
from __future__ import annotations

from typing import Any, Iterator

# Registry-engine nodes that reference an *image* model via `model_key` rather
# than a component file. Without this set the generic `"model_key" in inp`
# branch below labels their role "llm" (model_key is the LLM node's param name
# too), so a Flux2 image engine published through the legacy integrated
# image_generate node shows up as an LLM in the service overview.
_IMAGE_ENGINE_NODE_TYPES = {"image_generate"}


def _iter_nodes(snapshot: dict | None) -> Iterator[dict]:
    """Yield node dicts from either snapshot shape (dict-of-id or list)."""
    nodes = (snapshot or {}).get("nodes")
    if isinstance(nodes, dict):
        for node in nodes.values():
            if isinstance(node, dict):
                yield node
    elif isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict):
                yield node


def _node_type(node: dict) -> str:
    return str(node.get("class_type") or node.get("type") or "").strip()


def _node_inputs(node: dict) -> dict:
    # published snapshot stores node.data under "inputs"; editor uses "data".
    v = node.get("inputs")
    if isinstance(v, dict):
        return v
    v = node.get("data")
    return v if isinstance(v, dict) else {}


def extract_service_models(snapshot: dict | None) -> list[dict[str, Any]]:
    """Return the distinct engine refs a snapshot depends on.

    Each ref: {kind: 'engine', role, label, file: None, engine_key}.
    Order follows first appearance; dedup by engine key.
    """
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(ref: dict[str, Any], dedup_key: str) -> None:
        if dedup_key in seen:
            return
        seen.add(dedup_key)
        refs.append(ref)

    for node in _iter_nodes(snapshot):
        ntype = _node_type(node)
        ntlow = ntype.lower()
        inp = _node_inputs(node)

        # --- registry engines: editor `llm` (model_key) / `tts_engine` (engine) /
        #     trivial quick-provision `LLMEngine`/`TTSEngine`/`VLEngine` (engine) ---
        engine_key = inp.get("model_key") or inp.get("engine")
        if engine_key and (ntlow == "llm" or ntlow == "tts_engine"
                           or ntype.endswith("Engine") or "model_key" in inp):
            # Image-engine nodes are checked FIRST: image_generate carries
            # model_key, so it would otherwise fall into the llm branch below.
            role_guess = (
                "diffusion_models" if ntlow in _IMAGE_ENGINE_NODE_TYPES
                else "llm" if ntlow == "llm" or ntype.startswith("LLM") or "model_key" in inp
                else "tts" if ntlow == "tts_engine" or ntype.startswith("TTS")
                else None
            )
            add(
                {"kind": "engine", "role": role_guess, "label": str(engine_key),
                 "file": None, "engine_key": str(engine_key)},
                f"engine:{engine_key}",
            )
            continue

    return refs
