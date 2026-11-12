"""comfy 测试的全局状态隔离。

`comfy_bridge` 的渲染信号量 `_SEM` 和 `_running_task_id` 是**模块级**的(生产上靠它
保证 sidecar 一次只服务一个渲染)。测试里这会串味:某个用例的后台渲染任务如果没跑完
就结束(断言失败/超时/gate 没 set),信号量不释放,后面的用例全部拿不到 → 随机超时红。
2026-08-12 实测:单跑绿、全量跑偶发红一条 cancel 用例,就是这个。

每个用例前后重置这两个全局,让顺序无关。
"""
import asyncio

import pytest

import src.services.nodes.comfy_bridge as nb


@pytest.fixture(autouse=True)
def _isolate_bridge_globals():
    nb._SEM = asyncio.Semaphore(1)
    nb._running_task_id = None
    nb._running_since = None
    yield
    nb._SEM = asyncio.Semaphore(1)
    nb._running_task_id = None
    nb._running_since = None


# 生产 ComfyUI sidecar 的端口(见 infra/network.env)。comfy 用例一律走替身,真请求一个都不许发。
_PROD_COMFY_PORT = 8888


@pytest.fixture(autouse=True)
def _block_real_comfy_http(monkeypatch):
    """挡住发往 ComfyUI 的真实 HTTP(生产 :8888,以及 NOUS_COMFY_URL 指向的任何地址)。

    没打桩的代码路径(典型:桥剪枝时的 `get_node_infos`)会按 `NOUS_COMFY_URL` 真发请求;
    本机若在 env 里指向生产 sidecar,测试就会去碰正在出图的 ComfyUI。这里统一抛
    ConnectError —— 与「sidecar 不可达」同一形状,被测代码的降级路径照常生效。
    """
    import os
    from urllib.parse import urlsplit

    import httpx

    comfy = urlsplit(os.getenv("NOUS_COMFY_URL", "http://127.0.0.1:8188"))
    blocked = {(comfy.hostname, comfy.port)}
    real_send = httpx.AsyncClient.send

    async def _guarded_send(self, request, *args, **kwargs):
        url = request.url
        if url.port == _PROD_COMFY_PORT or (url.host, url.port) in blocked:
            raise httpx.ConnectError(f"测试里禁止连真 ComfyUI:{url}", request=request)
        return await real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", _guarded_send)
