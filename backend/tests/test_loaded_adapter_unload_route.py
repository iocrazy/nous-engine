"""`/loaded-adapter/unload` 路由顺序守卫。

从 test_unified_mgmt_combo_arch_pr2.py 挪来(2026-09-26 自建图像引擎删除,Task 4):那个文件
其余用例测 arch 推断 / ModularImageBackend,随图像引擎删掉;这条端点(spec §2.7 必须留)的
断言逐字保留。
"""
from __future__ import annotations

import pathlib

_SRC = pathlib.Path(__file__).parent.parent / "src"


def test_loaded_adapter_unload_route_before_param_route():
    """`/loaded-adapter/unload` 必须在 `/{name}/unload` **之前**(否则参数路由抢先匹配致 404)。"""
    src = (_SRC / "api/routes/engines.py").read_text()
    assert '"/loaded-adapter/unload"' in src
    assert "sup.client.unload_model(" in src
    assert src.index('"/loaded-adapter/unload"') < src.index('"/{name}/unload"')
