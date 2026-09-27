"""services/workflow_snapshot.py 纯逻辑 + 无循环依赖守卫(架构重构:抽出打破
workflow_publish ↔ services 循环)。"""
from types import SimpleNamespace

from src.services.workflow_snapshot import (
    NAME_RE,
    _build_snapshot,
    _detect_category,
    _node_ids,
    _node_types_by_id,
    _snapshot_hash,
)


def test_snapshot_hash_stable_and_order_independent():
    a = {"nodes": {"x": 1, "y": 2}}
    b = {"nodes": {"y": 2, "x": 1}}
    assert _snapshot_hash(a) == _snapshot_hash(b)
    assert _snapshot_hash(a).startswith("sha256:")


def test_node_ids_and_types_both_shapes():
    dict_snap = {"nodes": {"a": {"class_type": "llm"}, "b": {"class_type": "text_output"}}}
    list_snap = {"nodes": [{"id": "a", "type": "llm"}, {"id": "b", "class_type": "text_output"}]}
    assert _node_ids(dict_snap) == {"a", "b"} == _node_ids(list_snap)
    assert _node_types_by_id(dict_snap)["a"] == "llm"
    assert _node_types_by_id(list_snap)["a"] == "llm"


def test_detect_category_image_via_sink():
    assert _detect_category({"nodes": {"o": {"class_type": "image_output"}}}) == "image"
    assert _detect_category({"nodes": {"t": {"class_type": "text_output"}}}) is None


def test_build_snapshot_editor_list_to_api_dict():
    wf = SimpleNamespace(
        nodes=[{"id": "n1", "type": "llm", "data": {"model": "x"}, "meta": {"r": 1}}],
        edges=[{"from": "n1"}],
    )
    snap = _build_snapshot(wf)
    assert snap["schema"] == "comfy/api-1"
    assert snap["nodes"]["n1"]["class_type"] == "llm"
    assert snap["nodes"]["n1"]["inputs"] == {"model": "x"}
    assert snap["edges"] == [{"from": "n1"}]


def test_name_re():
    assert NAME_RE.match("nous-qwen3-8b")
    assert not NAME_RE.match("qwen3-8b")    # 缺 nous- 前缀
    assert not NAME_RE.match("nous-Qwen")   # 大写
    assert not NAME_RE.match("Nous-x")      # 前缀大写


def test_no_circular_import():
    """两个路由 + 新模块可无环 import,且共用同一份函数对象。"""
    import src.api.routes.services as s
    import src.api.routes.workflow_publish as wp
    import src.services.workflow_snapshot as ws
    assert s._snapshot_hash is wp._snapshot_hash is ws._snapshot_hash
    # services 不再 import workflow_publish(循环已断)
    import inspect
    assert "workflow_publish import" not in inspect.getsource(s._validate_exposed_against_snapshot)
