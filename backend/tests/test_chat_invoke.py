"""chat_invoke:从 openai_compat 抽出的非流式 chat 共用核心(spec 2026-09-26 skill-runs §4)。"""
from __future__ import annotations

import pytest

from src.api.chat_invoke import clamped_max_tokens


@pytest.mark.parametrize(("requested", "mml", "expected"), [
    (None, 4096, None),        # 没给就不设
    (100, 4096, 100),          # 没超不动
    (3584, 4096, 3584),        # 恰好 = mml-512 不动
    (4000, 4096, 3584),        # 超了夹到 mml-512
    (4000, 800, 400),          # mml-512 < mml//2 时取 mml//2
])
def test_clamped_max_tokens(requested, mml, expected):
    assert clamped_max_tokens(requested, mml) == expected


def test_openai_compat_keeps_private_aliases():
    """ollama_compat / responses / 既有测试仍从 openai_compat import 这些名字。"""
    from src.api import chat_invoke
    from src.api.routes import openai_compat as oc
    assert oc._maybe_inject_thinking is chat_invoke.inject_thinking
    assert oc._supports_thinking is chat_invoke.supports_thinking
    assert oc._post_consume_quota is chat_invoke.post_consume_quota
    assert oc._preflight_quota is chat_invoke.preflight_quota
    assert oc._granted_services is chat_invoke.granted_services
