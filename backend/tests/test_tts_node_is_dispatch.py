"""Lane F 回归：TTS 节点必须被归类为 dispatch 节点（进 runner 串行队列）。

spec §1.3 要解决的并发竞态正是靠「GPU 节点(现只剩 TTS)进 per-group 串行队列」根治。
若后续 Lane 把 tts_engine 误归类成 inline-HTTP 节点，会绕过串行队列、重新引入
adapter race —— 本测试锁死这个判定。

写法 A（Lane S 已合并 / 当前 master）：直接断言 node_exec_class(tts_engine) ==
"dispatch"；llm 节点是 inline。
写法 B（兜底）：直接断言 models.yaml 里 TTS 条目 model_type == "tts" 且
adapter_class 是 src.workers.tts_engines.* —— 不依赖任何 Lane，永远可跑。
"""

import pytest


def test_tts_engine_is_dispatch_and_llm_is_inline():
    """写法 A：Lane S 的 node_exec_class 把 tts_engine 判为 dispatch、llm 判为 inline。

    2026-09-26 从 test_tts_engine_node_is_dispatch_node 拆出:原用例还断言
    flux2_vae_decode 是 dispatch,该节点随自建图像引擎删除;TTS / llm 断言逐字保留。"""
    try:
        from src.services.node_routing import node_exec_class
    except ImportError:
        pytest.skip("Lane S node_routing not available - see 写法 B")
    assert node_exec_class("tts_engine") == "dispatch"
    # llm 节点是 inline（主进程直连 vLLM HTTP），不是 dispatch
    assert node_exec_class("llm") == "inline"


def test_tts_model_specs_typed_tts():
    """写法 B：configs/models.yaml 里 TTS adapter 条目 model_type == 'tts'。

    runner 内 ModelManager 按 spec.model_type 决定 adapter 形状；TTS 条目必须
    typed 'tts' 才会落进 TTS runner group。这条永远跑（不依赖 Lane S）。
    """
    from src.services.inference.registry import ModelRegistry

    reg = ModelRegistry("configs/models.yaml")
    tts_specs = reg.list_by_type("tts")
    assert tts_specs, "configs/models.yaml 应至少有一个 type: tts 的 adapter 条目"
    for spec in tts_specs:
        # adapter_class 是可解析的 dotted path（runner 内 ModelManager 要 import 它）
        assert "." in spec.adapter_class
        assert spec.adapter_class.startswith("src.workers.tts_engines.")


def test_tts_engine_node_registered():
    """写法 B 续：node registry 注册了 tts_engine handler。"""
    # 触发 registration：导入 audio nodes 模块会通过 @register 装饰器登记
    import src.services.nodes.audio  # noqa: F401
    from src.services.nodes.registry import get_node_class

    cls = get_node_class("tts_engine")
    assert cls is not None
