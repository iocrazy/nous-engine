"""`comfy/object_info.get_node_infos`:按节点类型取 object_info,带进程内 TTL 缓存。

取不到(sidecar 不可达 / 超时 / 不认识的类型)一律 None,绝不抛 —— 调用方(桥剪枝)对
None 走保守分支。
"""
from __future__ import annotations

import httpx
import pytest

import src.services.comfy.object_info as oi
from src.services.comfy.client import ComfyClient, ComfyError


class _Client:
    def __init__(self, infos: dict, fail: set[str] | None = None):
        self.infos = infos
        self.fail = fail or set()
        self.calls: list[str] = []

    async def node_info(self, class_type, *, timeout=5.0):
        self.calls.append(class_type)
        if class_type in self.fail:
            raise httpx.ConnectError("down")
        return self.infos.get(class_type)


@pytest.fixture(autouse=True)
def _reset():
    oi.reset_object_info_cache()
    yield
    oi.reset_object_info_cache()


@pytest.mark.asyncio
async def test_fetches_each_class_once_and_caches(monkeypatch):
    c = _Client({"A": {"input": {}}, "B": {"input": {"required": {}}}})
    monkeypatch.setattr(oi, "get_comfy_client", lambda: c)
    got = await oi.get_node_infos(["A", "B", "A"])
    assert got == {"A": {"input": {}}, "B": {"input": {"required": {}}}}
    await oi.get_node_infos(["A", "B"])
    assert sorted(c.calls) == ["A", "B"]  # 第二次全走缓存


@pytest.mark.asyncio
async def test_failure_and_unknown_map_to_none(monkeypatch):
    c = _Client({"A": {"input": {}}}, fail={"B"})
    monkeypatch.setattr(oi, "get_comfy_client", lambda: c)
    got = await oi.get_node_infos(["A", "B", "Missing"])
    assert got == {"A": {"input": {}}, "B": None, "Missing": None}


@pytest.mark.asyncio
async def test_negative_cache_expires_quickly(monkeypatch):
    c = _Client({}, fail={"B"})
    monkeypatch.setattr(oi, "get_comfy_client", lambda: c)
    now = [1000.0]
    monkeypatch.setattr(oi.time, "monotonic", lambda: now[0])
    await oi.get_node_infos(["B"])
    await oi.get_node_infos(["B"])
    assert c.calls == ["B"]  # 负缓存命中
    now[0] += oi.OBJECT_INFO_FAILURE_TTL_S + 1
    c.fail.clear()
    c.infos["B"] = {"input": {}}
    assert (await oi.get_node_infos(["B"]))["B"] == {"input": {}}
    assert c.calls == ["B", "B"]


@pytest.mark.asyncio
async def test_client_node_info_quotes_class_name_and_unwraps(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode()
        if "Missing" in seen["path"]:
            return httpx.Response(200, json={})
        if "Boom" in seen["path"]:
            return httpx.Response(500, text="x")
        return httpx.Response(200, json={"Image Comparer (rgthree)": {"output_node": True}})

    client = ComfyClient("http://sidecar")
    client._client = httpx.AsyncClient(
        base_url="http://sidecar", transport=httpx.MockTransport(handler))
    assert await client.node_info("Image Comparer (rgthree)") == {"output_node": True}
    assert seen["path"] == "/object_info/Image%20Comparer%20%28rgthree%29"
    assert await client.node_info("Missing") is None
    with pytest.raises(ComfyError):
        await client.node_info("Boom")
