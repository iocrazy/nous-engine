"""可选文件参数未传 → 剪掉整条支路(`comfy/graph_prune.prune_graph` 纯函数)。

级联规则按 object_info 的 required/optional 判:optional 或 autogrow 子键(`images.image_3`)
只删输入键,required 连节点一起删并继续级联,object_info 取不到保守删节点。
"""
from __future__ import annotations

import copy

import pytest

from src.services.comfy.graph_prune import (
    GraphPruneError,
    downstream_nodes,
    prune_graph,
)

OBJECT_INFO = {
    "LoadImage": {"input": {"required": {"image": [["a.png"], {}]}}, "output_node": False},
    "ImageScaleToTotalPixels": {
        "input": {"required": {"image": ["IMAGE", {}], "megapixels": ["FLOAT", {}]}},
        "output_node": False,
    },
    "TextEncodeQwenImage21": {
        "input": {
            "required": {"clip": ["CLIP", {}], "prompt": ["STRING", {}],
                         "images": ["COMFY_AUTOGROW_V3", {}]},
            "optional": {"vae": ["VAE", {}]},
        },
        "output_node": False,
    },
    "Image Comparer (rgthree)": {
        "input": {"required": {}, "optional": {"image_a": ["IMAGE"], "image_b": ["IMAGE"]}},
        "output_node": True,
    },
    "SaveImage": {"input": {"required": {"images": ["IMAGE", {}]}}, "output_node": True},
    "VAEDecode": {"input": {"required": {"samples": ["LATENT"], "vae": ["VAE"]}},
                  "output_node": False},
}


def _graph() -> dict:
    """两条参考图支路 → TextEncode(autogrow)→ VAEDecode → SaveImage;第 2 条还接一个对比器。"""
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": "main.png"}},
        "2": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["1", 0], "megapixels": 1}},
        "3": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}},
        "4": {"class_type": "ImageScaleToTotalPixels", "inputs": {"image": ["3", 0], "megapixels": 1}},
        "5": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["9", 0], "prompt": "p", "images.image_1": ["2", 0],
            "images.image_3": ["4", 0], "vae": ["9", 1]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 2], "vae": ["9", 1]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0]}},
        "8": {"class_type": "Image Comparer (rgthree)", "inputs": {
            "image_a": ["4", 0], "image_b": ["6", 0]}},
        "9": {"class_type": "Loader", "inputs": {}},
    }


def test_removes_loader_and_cascades_through_required_input():
    g = _graph()
    res = prune_graph(g, {"3"}, OBJECT_INFO)
    # LoadImage 3 删;ImageScale 4 的 image 是 required → 连带删
    assert "3" not in res.graph and "4" not in res.graph
    assert res.removed == frozenset({"3", "4"})
    # autogrow 子键只删键,TextEncode 本身保留
    assert "images.image_3" not in res.graph["5"]["inputs"]
    assert res.graph["5"]["inputs"]["images.image_1"] == ["2", 0]
    # 对比器的 image_a 是 optional → 只删键
    assert "image_a" not in res.graph["8"]["inputs"]
    assert res.graph["8"]["inputs"]["image_b"] == ["6", 0]
    assert ("5", "images.image_3") in res.dropped_inputs
    assert ("8", "image_a") in res.dropped_inputs
    # 主链与输出节点原样
    for nid in ("1", "2", "6", "7", "9"):
        assert res.graph[nid] == g[nid]


def test_input_graph_is_not_mutated():
    g = _graph()
    snapshot = copy.deepcopy(g)
    prune_graph(g, {"3"}, OBJECT_INFO)
    assert g == snapshot


def test_empty_remove_set_returns_equal_graph():
    g = _graph()
    res = prune_graph(g, set(), OBJECT_INFO)
    assert res.graph == g
    assert res.removed == frozenset()


def test_missing_object_info_is_conservative_and_reports_unknown():
    """下游节点类型的 object_info 取不到 → 删该节点并继续级联(宁可少一条支路也不喂占位图)。"""
    info = {k: v for k, v in OBJECT_INFO.items() if k != "ImageScaleToTotalPixels"}
    res = prune_graph(_graph(), {"3"}, info)
    assert "4" not in res.graph
    assert "ImageScaleToTotalPixels" in res.unknown_classes
    # 级联继续:TextEncode 的 autogrow 子键照删(TextEncode 的 info 在)
    assert "images.image_3" not in res.graph["5"]["inputs"]


def test_autogrow_subkey_only_dropped_even_without_object_info():
    """带 `.` 的 autogrow 子键是结构性的「可不接」:即便该节点的 info 取不到也只删键。"""
    info = {k: v for k, v in OBJECT_INFO.items() if k != "TextEncodeQwenImage21"}
    res = prune_graph(_graph(), {"3"}, info)
    assert "5" in res.graph
    assert "images.image_3" not in res.graph["5"]["inputs"]


def test_unknown_class_cascades_node_removal():
    """object_info 缺 VAEDecode → 它被保守删掉,继续级联到 SaveImage → 报输出链路错误。"""
    g = _graph()
    g["6"]["inputs"]["samples"] = ["4", 0]  # 让可选支路直连 VAEDecode
    info = {k: v for k, v in OBJECT_INFO.items() if k != "VAEDecode"}
    with pytest.raises(GraphPruneError, match="SaveImage"):
        prune_graph(g, {"3"}, info)


def test_required_chain_reaching_output_node_raises():
    """剪主图(required 一路到 SaveImage)= mapping 配错:可选参数其实是输出链路必需的。"""
    g = _graph()
    g["5"]["inputs"]["prompt"] = ["1", 0]  # 让主图经 required 输入直达编码器
    with pytest.raises(GraphPruneError, match="7"):
        prune_graph(g, {"1"}, OBJECT_INFO)


def test_preview_output_node_pruned_silently():
    """对比器/预览这类输出节点被剪不报错(不是产出端)。"""
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
        "2": {"class_type": "PreviewImage", "inputs": {"images": ["1", 0]}},
        "3": {"class_type": "SaveImage", "inputs": {"images": ["9", 0]}},
        "9": {"class_type": "Loader", "inputs": {}},
    }
    info = {**OBJECT_INFO,
            "PreviewImage": {"input": {"required": {"images": ["IMAGE"]}}, "output_node": True}}
    res = prune_graph(g, {"1"}, info)
    assert set(res.graph) == {"3", "9"}


def test_unknown_output_class_uses_fallback_list():
    """输出节点自己的 object_info 也取不到时,按已知产出类名兜底判输出。"""
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
        "2": {"class_type": "VHS_VideoCombine", "inputs": {"images": ["1", 0]}},
    }
    with pytest.raises(GraphPruneError, match="VHS_VideoCombine"):
        prune_graph(g, {"1"}, {"LoadImage": OBJECT_INFO["LoadImage"]})


def test_undeclared_input_name_is_conservative():
    """object_info 在但没声明这个输入(节点版本漂移)→ 同样保守删节点。"""
    g = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
        "2": {"class_type": "VAEDecode", "inputs": {"weird": ["1", 0]}},
        "3": {"class_type": "SaveImage", "inputs": {"images": ["9", 0]}},
        "9": {"class_type": "Loader", "inputs": {}},
    }
    res = prune_graph(g, {"1"}, OBJECT_INFO)
    assert "2" not in res.graph
    assert "3" in res.graph


def test_remove_ids_not_in_graph_are_ignored():
    res = prune_graph(_graph(), {"404"}, OBJECT_INFO)
    assert res.graph == _graph()


def test_downstream_nodes_is_transitive_closure():
    assert downstream_nodes(_graph(), {"3"}) == {"4", "5", "6", "7", "8"}
    assert downstream_nodes(_graph(), {"7"}) == set()
