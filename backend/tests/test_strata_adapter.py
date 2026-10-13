import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from src.services.inference.base import Message, TextRequest
from src.services.inference.llm_strata import (
    ManagedStrataAdapter,
    StrataAdapter,
    StrataSystemdController,
)


def _adapter(handler) -> StrataAdapter:
    return StrataAdapter(
        paths={},
        strata_base_url="http://127.0.0.1:18080",
        request_timeout=5,
        _transport=httpx.MockTransport(handler),
    )


def _request(**extra) -> TextRequest:
    return TextRequest(
        request_id="req-1",
        messages=[Message(role="user", content="hello")],
        model="strata",
        max_tokens=64,
        **extra,
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8080",
        "http://example.com:8080",
        "http://0.0.0.0:8080",
        "http://user:pass@127.0.0.1:8080",
        "http://127.0.0.1:8080/v1",
    ],
)
def test_rejects_non_local_origin(url):
    with pytest.raises(ValueError, match="本机 HTTP 地址"):
        StrataAdapter(paths={}, strata_base_url=url)


@pytest.mark.asyncio
async def test_load_requires_ready_strata_and_reads_context():
    async def handler(request: httpx.Request):
        assert request.url.path == "/health"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "service": "strata",
                "loaded": True,
                "max_context": 131072,
            },
        )

    adapter = _adapter(handler)
    await adapter.load("cuda:0")
    assert adapter.is_loaded
    assert adapter.max_model_len == 131072
    adapter.unload()


@pytest.mark.asyncio
async def test_load_does_not_trigger_strata_autoload():
    seen = []

    async def handler(request: httpx.Request):
        seen.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={"status": "ok", "service": "strata", "loaded": False},
        )

    adapter = _adapter(handler)
    with pytest.raises(RuntimeError, match="尚未加载"):
        await adapter.load("cuda:0")
    assert not adapter.is_loaded
    assert seen == [("GET", "/health")]
    adapter.unload()


@pytest.mark.asyncio
async def test_infer_uses_strata_reasoning_and_usage():
    captured = {}

    async def handler(request: httpx.Request):
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    adapter = _adapter(handler)
    result = await adapter.infer(_request(enable_thinking=False))
    assert captured["reasoning_effort"] == "none"
    assert captured["messages"] == [{"role": "user", "content": "hello"}]
    assert result.usage.input_tokens == 3
    assert result.usage.output_tokens == 2
    adapter.unload()


@pytest.mark.asyncio
async def test_infer_stream_yields_delta_and_usage():
    async def handler(request: httpx.Request):
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["stream_options"]["include_usage"] is True
        content = (
            'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})

    adapter = _adapter(handler)
    events = [event async for event in adapter.infer_stream(_request(enable_thinking=True))]
    assert [(event.type, event.payload) for event in events] == [
        ("delta", {"content": "hi"}),
        ("done", {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}),
    ]
    adapter.unload()


class _FakeController:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


def test_systemd_controller_uses_fixed_helper_argv():
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ctl = StrataSystemdController("iq3s", _run=run)
    ctl.start()
    ctl.stop()

    assert [call[0] for call in calls] == [
        ["sudo", "-n", "/usr/local/lib/nous-engine/strata-control", "start", "iq3s"],
        ["sudo", "-n", "/usr/local/lib/nous-engine/strata-control", "stop", "iq3s"],
    ]
    assert all(call[1]["check"] is False for call in calls)


@pytest.mark.parametrize("instance", ["", "IQ3S", "../x", "x.service", "x y", "-x"])
def test_systemd_controller_rejects_unsafe_instance(instance):
    with pytest.raises(ValueError, match="strata_instance"):
        StrataSystemdController(instance)


def test_systemd_controller_surfaces_bounded_failure():
    def run(*_args, **_kwargs):
        return SimpleNamespace(returncode=5, stdout="", stderr="denied")

    ctl = StrataSystemdController("iq3s", _run=run)
    with pytest.raises(RuntimeError, match=r"退出码 5.*denied"):
        ctl.start()


def test_managed_adapter_selects_context_instance():
    adapter = ManagedStrataAdapter(
        paths={},
        strata_base_url="http://127.0.0.1:18080",
        strata_instance="iq3s",
        max_model_len=262144,
        strata_context_presets={8192: "iq3s", 262144: "iq3s-256k"},
        _transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    assert adapter._controller.instance == "iq3s-256k"
    assert adapter.max_model_len == 262144


def test_managed_adapter_rejects_unconfigured_context():
    with pytest.raises(ValueError, match=r"可选: \[8192, 262144\]"):
        ManagedStrataAdapter(
            paths={},
            strata_base_url="http://127.0.0.1:18080",
            strata_instance="iq3s",
            max_model_len=524288,
            strata_context_presets={8192: "iq3s", 262144: "iq3s-256k"},
        )


@pytest.mark.asyncio
async def test_managed_adapter_starts_unit_then_waits_until_loaded():
    controller = _FakeController()
    checks = 0

    async def handler(_request: httpx.Request):
        nonlocal checks
        checks += 1
        return httpx.Response(
            200,
            json={
                "service": "strata",
                "loaded": checks >= 2,
                "max_context": 131072,
            },
        )

    adapter = ManagedStrataAdapter(
        paths={},
        strata_base_url="http://127.0.0.1:18080",
        strata_instance="iq3s",
        startup_timeout=1,
        startup_poll_interval=0.001,
        _controller=controller,
        _transport=httpx.MockTransport(handler),
    )
    await adapter.load("cuda:1")

    assert controller.started == 1
    assert checks == 2
    assert adapter.is_loaded
    assert adapter.max_model_len == 131072

    adapter.unload()
    assert controller.stopped == 1
    assert not adapter.is_loaded


@pytest.mark.asyncio
async def test_managed_adapter_timeout_stops_unit_and_closes_client():
    controller = _FakeController()

    async def handler(_request: httpx.Request):
        return httpx.Response(200, json={"service": "strata", "loaded": False})

    adapter = ManagedStrataAdapter(
        paths={},
        strata_base_url="http://127.0.0.1:18080",
        strata_instance="iq3s",
        startup_timeout=0.01,
        startup_poll_interval=0.001,
        _controller=controller,
        _transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RuntimeError, match="未就绪"):
        await adapter.load()

    assert controller.started == 1
    assert controller.stopped == 1
    assert adapter._client is None


@pytest.mark.asyncio
async def test_managed_adapter_start_failure_closes_client():
    class FailedController(_FakeController):
        def start(self):
            raise RuntimeError("start denied")

    adapter = ManagedStrataAdapter(
        paths={},
        strata_base_url="http://127.0.0.1:18080",
        strata_instance="iq3s",
        _controller=FailedController(),
        _transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    with pytest.raises(RuntimeError, match="start denied"):
        await adapter.load()
    assert adapter._client is None


@pytest.mark.asyncio
async def test_managed_adapter_rejects_health_context_mismatch():
    controller = _FakeController()

    async def handler(_request: httpx.Request):
        return httpx.Response(
            200,
            json={"service": "strata", "loaded": True, "max_context": 8192},
        )

    adapter = ManagedStrataAdapter(
        paths={},
        strata_base_url="http://127.0.0.1:18080",
        strata_instance="iq3s-256k",
        max_model_len=262144,
        startup_timeout=1,
        startup_poll_interval=0.001,
        _controller=controller,
        _transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RuntimeError, match="期望 262144,实际 8192"):
        await adapter.load()

    assert controller.stopped == 1
    assert adapter._client is None
    assert not adapter.is_loaded


@pytest.mark.asyncio
async def test_managed_adapter_cancel_stops_unit_and_closes_client():
    controller = _FakeController()

    async def handler(_request: httpx.Request):
        return httpx.Response(200, json={"service": "strata", "loaded": False})

    adapter = ManagedStrataAdapter(
        paths={},
        strata_base_url="http://127.0.0.1:18080",
        strata_instance="iq3s",
        startup_timeout=10,
        startup_poll_interval=1,
        _controller=controller,
        _transport=httpx.MockTransport(handler),
    )
    task = asyncio.create_task(adapter.load())
    while controller.started == 0:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert controller.stopped == 1
    assert adapter._client is None
