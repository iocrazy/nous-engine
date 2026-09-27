"""runner 派发契约里与 TTS 相关、在图像引擎删除后仍成立的断言。

从已删的 test_seedvr2_runner_dispatch.py 拆出(自建图像引擎删除 Task 3):
原文件的混合用例同时断言 image/upscale 与非图像行为,图像部分随 runner 图像分支
一起删,这里保住非图像部分。
"""
from __future__ import annotations

import pytest

from src.runner import protocol as P
from src.runner.runner_process import _build_request


def _node(inputs, node_type):
    return P.RunNode(task_id=7, node_id="n", node_type=node_type, model_key=None, inputs=inputs)


def test_unknown_node_type_raises_expected_tts():
    """非 tts 的 node_type → ValueError,提示唯一合法 role 是 tts。"""
    with pytest.raises(ValueError, match="expected tts"):
        _build_request(_node({}, node_type="bogus"))


def test_workflow_executor_tts_role_equals_group():
    """tts_engine 的 runner role 与 group_id 相同(无解耦)。"""
    from src.services.workflow_executor import (
        _NODE_TYPE_TO_GROUP_ID,
        _NODE_TYPE_TO_RUNNER_ROLE,
    )

    assert _NODE_TYPE_TO_RUNNER_ROLE["tts_engine"] == _NODE_TYPE_TO_GROUP_ID["tts_engine"]
