import json
from contextlib import asynccontextmanager

import httpx
import pytest

from src.services.comfy.client import ComfyClient, ComfyError, translate_prompt_error


def make_client(handler) -> ComfyClient:
    c = ComfyClient(base_url="http://comfy.test")
    c._client = httpx.AsyncClient(  # noqa: SLF001 — 测试注入 MockTransport
        transport=httpx.MockTransport(handler), base_url="http://comfy.test")
    return c


@pytest.mark.asyncio
async def test_submit_returns_prompt_id():
    def handler(req):
        assert req.url.path == "/prompt"
        assert json.loads(req.content)["client_id"]
        return httpx.Response(200, json={"prompt_id": "abc123"})
    assert await make_client(handler).submit({"1": {"class_type": "X", "inputs": {}}}) == "abc123"


@pytest.mark.asyncio
async def test_watch_progress_keeps_only_matching_comfy_prompt(monkeypatch):
    async def messages():
        yield b"binary preview"
        yield json.dumps({"type": "progress", "data": {"prompt_id": "other", "node": "8", "value": 3, "max": 20}})
        yield json.dumps({"type": "executing", "data": {"prompt_id": "p1", "node": "5"}})
        yield json.dumps({"type": "progress", "data": {"prompt_id": "p1", "node": "5", "value": 4, "max": 20}})
        yield json.dumps({"type": "progress_state", "data": {"prompt_id": "p1", "nodes": {
            "5": {"state": "finished", "value": 20, "max": 20},
            "6": {"state": "running", "display_node_id": "50", "value": 0, "max": 1},
        }}})
        yield json.dumps({"type": "progress_state", "data": {"prompt_id": "p1", "nodes": {
            "6": {"state": "running", "display_node_id": "50", "value": 7, "max": 18},
        }}})

    @asynccontextmanager
    async def websocket(url, **_kwargs):
        assert url.startswith("ws://comfy.test/ws?clientId=")
        yield messages()

    monkeypatch.setattr("src.services.comfy.client.connect", websocket)
    received = []
    await make_client(lambda _: None).watch_progress("p1", received.append)
    assert received == [
        {"phase": "running", "node": "5"}, {"phase": "running", "node": "5", "step": 4, "total": 20},
        {"phase": "running", "node": "50"}, {"phase": "running", "node": "50", "step": 7, "total": 18},
    ]


@pytest.mark.asyncio
async def test_submit_validation_error_translated():
    def handler(req):
        return httpx.Response(400, json={"error": {"message": "invalid prompt"}, "node_errors": {
            "138": {"errors": [{"message": "Required input is missing", "extra_info": {"input_name": "text"}}],
                     "class_type": "PrimitiveStringMultiline"}}})
    with pytest.raises(ComfyError) as e:
        await make_client(handler).submit({})
    assert "138" in e.value.message and "text" in e.value.message


def _queue_body(running=(), pending=()) -> dict:
    """`/queue` 的真实形状:两个列表,每项 `[number, prompt_id, graph, extra_data, outputs]`。"""
    return {
        "queue_running": [[0, pid, {}, {}, []] for pid in running],
        "queue_pending": [[0, pid, {}, {}, []] for pid in pending],
    }


@pytest.mark.asyncio
async def test_wait_polls_until_history_present():
    calls = {"history": 0}
    def handler(req):
        if req.url.path == "/queue":
            return httpx.Response(200, json=_queue_body(running=["p1"]))
        calls["history"] += 1
        body = {} if calls["history"] < 3 else {"p1": {"outputs": {"92": {"images": []}}}}
        return httpx.Response(200, json=body)
    hist = await make_client(handler).wait("p1", timeout_s=10, interval_s=0)
    assert "outputs" in hist and calls["history"] == 3


@pytest.mark.asyncio
async def test_wait_raises_when_should_abort_returns_true():
    """取消探测返回 True → 立刻退出(不等到 NOUS_COMFY_TIMEOUT),错误信息说明是取消。"""
    def handler(req):
        if req.url.path == "/queue":
            return httpx.Response(200, json=_queue_body(running=["p1"]))
        return httpx.Response(200, json={})
    probes = {"n": 0}
    async def should_abort():
        probes["n"] += 1
        return probes["n"] >= 2
    with pytest.raises(ComfyError, match="已取消"):
        await make_client(handler).wait(
            "p1", timeout_s=10, interval_s=0, should_abort=should_abort, abort_check_every=1)
    assert probes["n"] == 2


@pytest.mark.asyncio
async def test_wait_checks_abort_at_low_frequency():
    """探测是**每 abort_check_every 轮一次**,不是每轮 —— 4 小时的渲染每 2s 打一次 DB
    就是 7200 次纯轮询查询,而晚 10 秒发现取消对用户无差别。"""
    calls = {"history": 0}
    def handler(req):
        if req.url.path == "/queue":
            return httpx.Response(200, json=_queue_body(running=["p1"]))
        calls["history"] += 1
        body = {} if calls["history"] < 11 else {"p1": {"outputs": {}}}
        return httpx.Response(200, json=body)
    probes = {"n": 0}
    async def should_abort():
        probes["n"] += 1
        return False
    await make_client(handler).wait(
        "p1", timeout_s=10, interval_s=0, should_abort=should_abort)
    assert calls["history"] == 11
    assert probes["n"] == 3, "11 轮轮询只该在第 0/5/10 轮查 3 次 DB"


@pytest.mark.asyncio
async def test_wait_detects_prompt_lost_from_queue():
    """sidecar 重启 / 队列被清:prompt 既不在 /queue 也永远不进 /history —— 必须在
    宽限期后判丢失并抛错,而不是占着渲染信号量干等 4 小时(2026-09-03 事故)。"""
    calls = {"history": 0}
    def handler(req):
        if req.url.path == "/queue":
            return httpx.Response(200, json=_queue_body())
        calls["history"] += 1
        return httpx.Response(200, json={})
    with pytest.raises(ComfyError, match="丢失"):
        await make_client(handler).wait("p1", timeout_s=10, interval_s=0)
    # 3 轮宽限(每轮一次 history)+ 判定前的最后一次确认 = 4
    assert calls["history"] == 4


@pytest.mark.asyncio
async def test_wait_grace_window_right_after_submit_not_flagged_lost():
    """刚 submit 完有个极短窗口:ComfyUI 收下了 prompt 但队列快照还没有它、history 也
    还没有。这不能被判成"丢失"。"""
    rounds = {"queue": 0, "history": 0}
    def handler(req):
        if req.url.path == "/queue":
            rounds["queue"] += 1
            # 前两轮两边都还没有,之后才进 running。
            return httpx.Response(200, json=_queue_body(
                running=["p1"] if rounds["queue"] > 2 else []))
        rounds["history"] += 1
        body = {} if rounds["history"] < 5 else {"p1": {"outputs": {"92": {}}}}
        return httpx.Response(200, json=body)
    hist = await make_client(handler).wait("p1", timeout_s=10, interval_s=0)
    assert "outputs" in hist


@pytest.mark.asyncio
async def test_wait_queue_unreachable_is_not_treated_as_lost():
    """sidecar 不可达(重启窗口本身)= 状态未知,不是"任务没了" —— 继续轮询,最后
    落到超时错误而不是丢失。"""
    def handler(req):
        if req.url.path == "/queue":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={})
    with pytest.raises(ComfyError, match="超时"):
        await make_client(handler).wait("p1", timeout_s=0.05, interval_s=0)


@pytest.mark.asyncio
async def test_wait_lost_check_honors_history_that_just_appeared():
    """判丢失前的最后一次 history 确认:任务可能正好在"查队列"和"判定"之间跑完
    (出队 → 进 history),这时必须返回结果而不是报丢失。"""
    calls = {"history": 0}
    def handler(req):
        if req.url.path == "/queue":
            return httpx.Response(200, json=_queue_body())
        calls["history"] += 1
        body = {"p1": {"outputs": {"92": {}}}} if calls["history"] >= 4 else {}
        return httpx.Response(200, json=body)
    hist = await make_client(handler).wait("p1", timeout_s=10, interval_s=0)
    assert "outputs" in hist and calls["history"] == 4


@pytest.mark.asyncio
async def test_wait_timeout_raises():
    def handler(req):
        return httpx.Response(200, json={})
    with pytest.raises(ComfyError, match="超时"):
        await make_client(handler).wait("p1", timeout_s=0.01, interval_s=0)


@pytest.mark.asyncio
async def test_health_offline_on_connect_error():
    def handler(req):
        raise httpx.ConnectError("refused")
    h = await make_client(handler).health()
    assert h["online"] is False


def test_translate_prompt_error_fallback_plain_text():
    msg = translate_prompt_error(500, "Internal Server Error")
    assert "HTTP 500" in msg
