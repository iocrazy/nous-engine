"""Workflow 快照的纯逻辑:构建 / 哈希 / 节点提取 / 类别检测。

抽出来打破 `api/routes/workflow_publish.py` ↔ `api/routes/services.py` 的循环依赖
(此前两文件互相 import,`_snapshot_hash` 还各定义一份;services 靠函数内 lazy import
苟活)。本模块只依赖 stdlib + `models.workflow.Workflow`,**不 import 任何 api 层**,
两个路由文件都向下依赖它。下划线命名保留(与旧调用点一致,减少 diff),视为本模块的
包内共享 API。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from src.models.workflow import Workflow

# 服务名校验(路由与发布共用):小写字母开头,a-z0-9-,总长 2-63。
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")

# category → 计量维度。发布/快速置备/类别回填共用的纯数据映射。
_METER_DIM_BY_CATEGORY = {"llm": "tokens", "tts": "chars", "vl": "calls", "image": "images"}

# 暴露输出字段白名单:`_IMAGE_NODE_TYPES` 里的产出节点只能引用这些字段。
# image_generate / flux2_vae_decode 已随原生图像引擎删除,不再是可创建的节点类型;
# 这两个集合(连同下面的 `_IMAGE_DETECT_TYPES`)按它们当年的行为原样保留,只用来
# 分类发布时冻结的旧快照,不代表当前仍存在这些节点。字段清单来自它们当年发的
# {image_url, media_type, width, height, image_uuid, image_expires};image_generate
# 另带 steps/seed/loras/duration_ms。取并集作允许集。
_IMAGE_OUTPUT_FIELDS = {
    "image_url",
    "image",          # base64 fallback for dev mode
    "image_uuid",
    "image_expires",
    "media_type",
    "width",
    "height",
    "steps",
    "seed",
    "loras",
    "duration_ms",
}

# 暴露输出字段守卫作用域:仅产出节点终端。
_IMAGE_NODE_TYPES = {"flux2_vae_decode"}

# 类别检测集 —— 与 `_IMAGE_NODE_TYPES` 不同。检测以规范的图像 *sink* `image_output`
# 为主信号:所有图像流都以它收尾(不论产出方式)。只认单一 producer 的旧集会把
# image_generate 服务误判成 "app"。产出终端仍留作无 image_output 快照的兜底。
_IMAGE_DETECT_TYPES = {
    "image_output",       # canonical image sink — primary, producer-agnostic signal
    "flux2_vae_decode",   # component-path terminus
    "image_generate",     # legacy integrated image node (Family B frozen snapshots)
    "seedvr2_upscale",    # image→image upscale terminus
}


def _snapshot_hash(snapshot: dict) -> str:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _node_ids(snapshot: dict) -> set[str]:
    """Pull node ids out of either the api-style dict or the editor-style list."""
    nodes = snapshot.get("nodes")
    if isinstance(nodes, dict):
        return {str(k) for k in nodes.keys()}
    if isinstance(nodes, list):
        return {str(n.get("id")) for n in nodes if isinstance(n, dict) and n.get("id") is not None}
    return set()


def _node_types_by_id(snapshot: dict) -> dict[str, str]:
    """Map node_id → class_type for either snapshot shape."""
    out: dict[str, str] = {}
    nodes = snapshot.get("nodes")
    if isinstance(nodes, dict):
        for nid, node in nodes.items():
            ct = node.get("class_type") if isinstance(node, dict) else None
            if ct:
                out[str(nid)] = str(ct)
    elif isinstance(nodes, list):
        for n in nodes:
            if not isinstance(n, dict):
                continue
            nid = n.get("id")
            ct = n.get("class_type") or n.get("type")
            if nid is not None and ct:
                out[str(nid)] = str(ct)
    return out


def _detect_category(snapshot: dict) -> str | None:
    """Heuristic per-modality detection from the snapshot's node types.

    Returned value drops into ServiceInstance.category + meter_dim. We only
    auto-detect image here (via the image sink node, see `_IMAGE_DETECT_TYPES`)
    — LLM/TTS/VL flow through the explicit body.category path that
    quick-provision already controls. Returns None when no image sink is found,
    so callers fall back to body.category or "app" without clobbering an
    explicitly-locked modality.
    """
    types = set(_node_types_by_id(snapshot).values())
    if types & _IMAGE_DETECT_TYPES:
        return "image"
    return None


def _build_snapshot(wf: Workflow) -> dict[str, Any]:
    """Render the workflow's working state into the api-shape we freeze.

    Editor stores nodes as a list with explicit ids; api-shape is a dict
    keyed by node id. We always emit api-shape so consumers (executor,
    schema validator) only have to handle one form.
    """
    nodes_dict: dict[str, Any] = {}
    for node in (wf.nodes or []):
        nid = node.get("id")
        if nid is None:
            continue
        nodes_dict[str(nid)] = {
            "class_type": node.get("type") or node.get("class_type"),
            "inputs": node.get("data", node.get("inputs", {})),
            "_meta": node.get("meta", {}),
        }
    return {
        "schema": "comfy/api-1",
        "nodes": nodes_dict,
        "edges": wf.edges or [],
    }
