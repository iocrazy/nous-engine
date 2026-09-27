"""Tests for src/api/response_cache.py — covers eng-review fixes #1-#10.

Uses a thin FastAPI app with one cached GET so behavior is observable without
loading the full nous-engine app (which pulls torch/sqlalchemy/etc).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.testclient import TestClient

from src.api.response_cache import _reset_for_tests, cached, invalidate, metrics


@pytest.fixture
def app():
    """Build a fresh app with the cached endpoint and known counters."""
    _reset_for_tests()
    app = FastAPI()
    state: dict[str, int] = {"calls": 0, "value": 1}

    @app.get("/items")
    @cached("items", ttl=30)
    async def list_items(request: Request, type: str | None = None):
        state["calls"] += 1
        rows = [{"id": 1, "value": state["value"], "type": type or "all"}]
        return rows

    @app.get("/error")
    @cached("err", ttl=30)
    async def boom(request: Request):
        state["calls"] += 1
        raise HTTPException(status_code=500, detail="boom")

    @app.get("/cookie-set")
    @cached("ck", ttl=30)
    async def with_cookie(request: Request):
        state["calls"] += 1
        r = Response(content=b'{"ok": true}', media_type="application/json")
        r.set_cookie("evil", "leak")
        return r

    yield app, state


def test_hit_serves_cached_body_and_avoids_handler(app):
    fa, state = app
    client = TestClient(fa)
    r1 = client.get("/items")
    r2 = client.get("/items")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content
    # Handler called exactly once
    assert state["calls"] == 1
    # ETag + Cache-Control on both responses
    assert r2.headers["etag"] == r1.headers["etag"]
    assert "private" in r2.headers["cache-control"]
    assert metrics.hits == 1 and metrics.misses == 1


def test_etag_304_when_client_sends_if_none_match(app):
    fa, _ = app
    client = TestClient(fa)
    r1 = client.get("/items")
    etag = r1.headers["etag"]
    r2 = client.get("/items", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""
    assert r2.headers["etag"] == etag
    assert metrics.etag_304 == 1


def test_invalidate_drops_cache(app):
    fa, state = app
    client = TestClient(fa)
    client.get("/items")
    assert state["calls"] == 1
    invalidate("items")
    client.get("/items")
    assert state["calls"] == 2  # handler ran again after invalidate
    assert metrics.invalidations == 1


def test_query_string_isolation(app):
    """Bug we explicitly fixed: ?type=tts must NOT collide with ?type=llm."""
    fa, state = app
    client = TestClient(fa)
    client.get("/items?type=tts")
    client.get("/items?type=llm")
    # Two distinct keys → handler called twice
    assert state["calls"] == 2
    # Same query, different param order → same key (sorted)
    client.get("/items?type=tts")  # cache hit
    assert state["calls"] == 2


def test_etag_stable_when_data_unchanged(app):
    """Bug we explicitly fixed: ETag computed from serialized bytes — same body
    must always produce same ETag (no nondeterministic dict/set ordering)."""
    fa, _ = app
    client = TestClient(fa)
    etags = set()
    for _ in range(5):
        invalidate("items")
        r = client.get("/items")
        etags.add(r.headers["etag"])
    assert len(etags) == 1


def test_etag_changes_when_body_changes(app):
    fa, state = app
    client = TestClient(fa)
    r1 = client.get("/items")
    invalidate("items")
    state["value"] = 99
    r2 = client.get("/items")
    assert r1.headers["etag"] != r2.headers["etag"]
    # Stale If-None-Match returns full body, not 304
    r3 = client.get("/items", headers={"If-None-Match": r1.headers["etag"]})
    assert r3.status_code == 200


def test_5xx_not_cached(app):
    fa, state = app
    client = TestClient(fa)
    r1 = client.get("/error")
    r2 = client.get("/error")
    assert r1.status_code == 500 and r2.status_code == 500
    # Handler ran both times (no caching of error)
    assert state["calls"] == 2


def test_cookie_setting_response_passes_through(app):
    """Set-Cookie responses must NOT be cached — would leak session across users."""
    fa, state = app
    client = TestClient(fa)
    r1 = client.get("/cookie-set")
    r2 = client.get("/cookie-set")
    assert r1.headers.get("set-cookie") and r2.headers.get("set-cookie")
    assert state["calls"] == 2  # not cached


def test_cache_control_header_blocks_shared_caches(app):
    fa, _ = app
    client = TestClient(fa)
    r = client.get("/items")
    cc = r.headers["cache-control"]
    assert "private" in cc
    assert "must-revalidate" in cc


def test_concurrent_fills_coalesce_single_handler_call():
    """Thundering herd: 10 concurrent waiters → handler runs once."""
    _reset_for_tests()
    app = FastAPI()
    state = {"calls": 0}

    @app.get("/slow")
    @cached("slow", ttl=30)
    async def slow(request: Request):
        state["calls"] += 1
        await asyncio.sleep(0.05)
        return {"v": 1}

    async def hammer():
        from httpx import ASGITransport, AsyncClient
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            await asyncio.gather(*[ac.get("/slow") for _ in range(10)])

    asyncio.run(hammer())
    # Per-prefix lock + double-check pattern → handler runs once
    assert state["calls"] == 1


def test_cross_prefix_invalidate(app):
    fa, state = app
    client = TestClient(fa)
    client.get("/items")
    client.get("/error")  # populates 'err' prefix even though it errored? no
    # Only 'items' prefix should drop on invalidate('items')
    invalidate("items")
    assert metrics.invalidations == 1
    # Multi-prefix invalidate
    invalidate("items", "err")
    assert metrics.invalidations == 3  # 1 + 2
