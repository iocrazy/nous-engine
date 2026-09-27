"""可选文件参数未传时,从 ComfyUI API-format graph 里剪掉它的整条支路(纯函数)。

用途:mapping 条目标了 `omit_when_empty` 的文件类参数(如 qwen21 编辑的「参考图 2…8」),
调用方没传时,桥把它指向的 LoadImage 节点交给这里删掉,再顺着连线往下游级联 —— 不这样做,
LoadImage 会把模板里烤死的占位图照样喂进模型(见 docs/comfy-bridge-notes.md)。

级联规则(下游节点某输入连到了被删节点):
- 该输入在 object_info 里是 **optional**,或是 autogrow 子键(键名带 `.`,如
  `images.image_3` —— ComfyUI 按「实际连了哪些子键」展开 autogrow,缺的子键就是没接)
  → 只删这个输入键,节点保留;
- **required** → 该节点也删,继续级联;
- object_info 取不到这个节点类型,或 object_info 里压根没声明这个输入名(节点版本漂移)
  → **保守删节点**并继续级联(宁可少一条支路,也不喂占位图),记进 `unknown_classes`
  由调用方打 warning。

级联删到**产出端**(object_info `output_node: true` 且不是预览/对比类;info 取不到时按
已知产出类名兜底)→ 抛 `GraphPruneError`:这说明该「可选」参数实际是输出链路必需的,
mapping 配错了,绝不静默提交一张没有产出端的图。上游只剩孤儿的节点不必清理 —— ComfyUI
只从输出节点反向执行,够不着的节点既不校验也不跑。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from src.services.comfy.outputs import is_preview_class

# object_info 取不到输出节点自身的类型时,按类名兜底判「产出端」。
FALLBACK_OUTPUT_CLASSES = frozenset({
    "SaveImage", "Image Save", "SaveImageWebsocket", "SaveAnimatedWEBP", "SaveAnimatedPNG",
    "VHS_VideoCombine", "SaveVideo", "SaveWEBM", "SaveAudio",
})


class GraphPruneError(ValueError):
    """剪枝级联到了产出端 —— mapping 把输出链路必需的输入标成了可省略。"""


@dataclass(frozen=True)
class PruneResult:
    graph: dict[str, Any]
    removed: frozenset[str]
    # (节点 id, 输入名):只删了键、节点保留的那些输入。
    dropped_inputs: tuple[tuple[str, str], ...]
    # 因 object_info 缺失 / 未声明输入而被保守删除的节点类型。
    unknown_classes: frozenset[str]


def _link_source(value: Any) -> str | None:
    """API-format 里的连线是 `[源节点 id, 输出下标]`;不是连线返回 None。"""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
        return str(value[0])
    return None


def _consumers(graph: Mapping[str, Any], source_id: str) -> list[tuple[str, str]]:
    """连到 `source_id` 的所有 (下游节点 id, 输入名)。"""
    out: list[tuple[str, str]] = []
    for nid, node in graph.items():
        inputs = (node or {}).get("inputs") or {}
        for name, value in inputs.items():
            if _link_source(value) == source_id:
                out.append((str(nid), name))
    return out


def downstream_nodes(graph: Mapping[str, Any], roots: Iterable[str]) -> set[str]:
    """`roots` 的全部下游节点(传递闭包,不含 roots 本身)。桥据此只取需要的 object_info。"""
    roots_set = {str(r) for r in roots}
    seen: set[str] = set()
    frontier = list(roots_set)
    while frontier:
        src = frontier.pop()
        for nid, _name in _consumers(graph, src):
            if nid not in seen and nid not in roots_set:
                seen.add(nid)
                frontier.append(nid)
    return seen


def _input_kind(info: Mapping[str, Any] | None, input_name: str) -> str:
    """→ "optional" | "required" | "unknown"。"""
    if "." in input_name:
        # autogrow / dynamic 子键:没接的子键 ComfyUI 当「没传」,只删键即可。
        return "optional"
    if not isinstance(info, Mapping):
        return "unknown"
    spec = info.get("input") or {}
    if input_name in (spec.get("optional") or {}):
        return "optional"
    if input_name in (spec.get("required") or {}):
        return "required"
    return "unknown"


def _is_output(class_type: str, info: Mapping[str, Any] | None) -> bool:
    if is_preview_class(class_type):
        return False
    if isinstance(info, Mapping) and "output_node" in info:
        return bool(info.get("output_node"))
    return class_type in FALLBACK_OUTPUT_CLASSES


def prune_graph(
    graph: Mapping[str, Any],
    remove: Iterable[str],
    object_info: Mapping[str, Mapping[str, Any] | None],
) -> PruneResult:
    """删掉 `remove` 里的节点并按 object_info 级联,返回新 graph(不改入参)。

    `object_info` 按 class_type 取值(即 `/object_info/{class}` 响应里那个 value);
    缺键或值为 None = 取不到。
    """
    work = deepcopy(dict(graph))
    queue = [str(r) for r in remove if str(r) in work]
    removed: set[str] = set()
    dropped: list[tuple[str, str]] = []
    unknown: set[str] = set()

    while queue:
        nid = queue.pop(0)
        if nid in removed:
            continue
        removed.add(nid)
        for consumer, input_name in _consumers(work, nid):
            if consumer in removed:
                continue
            ct = str(work[consumer].get("class_type") or "")
            kind = _input_kind(object_info.get(ct), input_name)
            if kind == "optional":
                del work[consumer]["inputs"][input_name]
                dropped.append((consumer, input_name))
                continue
            if kind == "unknown":
                unknown.add(ct)
            queue.append(consumer)

    outputs_hit = sorted(
        (nid, str(work[nid].get("class_type") or ""))
        for nid in removed
        if _is_output(str(work[nid].get("class_type") or ""),
                      object_info.get(str(work[nid].get("class_type") or "")))
    )
    if outputs_hit:
        hit = "、".join(f"{nid}({ct})" for nid, ct in outputs_hit)
        raise GraphPruneError(
            f"剪掉未传的可选参数会连带删掉输出节点 {hit} —— 该参数实际是输出链路必需的,"
            "mapping 不该给它标 omit_when_empty")

    new_graph = {nid: node for nid, node in work.items() if nid not in removed}
    return PruneResult(
        graph=new_graph,
        removed=frozenset(removed),
        dropped_inputs=tuple(dropped),
        unknown_classes=frozenset(unknown),
    )
