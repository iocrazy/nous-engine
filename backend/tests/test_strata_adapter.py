import json

import httpx
import pytest

from src.services.inference.base import Message, TextRequest
from src.services.inference.llm_strata import StrataAdapter


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
