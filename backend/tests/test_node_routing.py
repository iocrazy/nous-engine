"""Lane S: 节点分流判定（dispatch vs inline-HTTP）。"""
import pytest

from src.services.node_routing import node_exec_class, DISPATCH_NODE_TYPES


def test_llm_node_is_inline():
    """llm 节点走 inline —— 现有 LLMNode 已经 HTTP 调 vLLM。"""
    assert node_exec_class("llm") == "inline"


def test_tts_engine_is_dispatch():
    assert node_exec_class("tts_engine") == "dispatch"


@pytest.mark.parametrize("node_type", [
    "text_input", "text_output", "prompt_template", "agent",
    "if_else", "python_exec", "passthrough", "output",
])
def test_cpu_nodes_are_inline(node_type):
    """纯 CPU / 逻辑节点走 inline。"""
    assert node_exec_class(node_type) == "inline"


def test_unknown_node_defaults_inline():
    """未知节点类型（含插件节点）默认 inline —— 保守：不假设它需要 GPU runner。"""
    assert node_exec_class("some_plugin_node") == "inline"


def test_dispatch_set_is_explicit_tts_only():
    """DISPATCH_NODE_TYPES 是显式白名单，新增 GPU 节点必须在此登记。

    2026-09-26:flux2_vae_decode / seedvr2_upscale 随自建图像引擎删除,白名单只剩 tts_engine。"""
    assert DISPATCH_NODE_TYPES == {"tts_engine"}
