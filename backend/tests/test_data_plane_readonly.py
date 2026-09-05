"""spec 2026-09-05 §4/§5/§6:数据面对模型放置只读。

1) 静态守卫:五个兼容路由模块不得引用任何加载能力(与 _placement.py 三条不变式同路数)。
2) 行为:已授权但未加载 → 503 model_not_ready,且 load_model 未被调用。
3) /v1/models 只列就绪的 model 服务;非 model 服务照旧;owned_by == "nous-engine"。
"""
from __future__ import annotations

import inspect
import io
import wave
from unittest.mock import MagicMock

import bcrypt
import pytest

from src.models.api_gateway import ApiKeyGrant
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance

DATA_PLANE_MODULES = (
    "src.api.routes.openai_compat",
    "src.api.routes.anthropic_compat",
    "src.api.routes.ollama_compat",
    "src.api.routes.responses",
    "src.api.routes.context_cache",
)


def test_data_plane_modules_have_no_load_capability():
    import importlib
    for name in DATA_PLANE_MODULES:
        src = inspect.getsource(importlib.import_module(name))
        assert "ensure_vllm_base_url" not in src, f"{name} 仍引用懒加载 helper"
        assert ".load_model(" not in src, f"{name} 直接调 load_model —— 数据面不得改变放置"
        # 另外两扇「按需加载」的门:两者都会在请求路径上把冷模型拉起来。
        assert ".get_loaded_adapter(" not in src, f"{name} 引用 get_loaded_adapter —— 那是按需加载门"
        assert "get_or_load" not in src, f"{name} 引用 get_or_load —— 那是按需加载门"
        assert "get_vllm_base_url" in src, f"{name} 应只经只读的 get_vllm_base_url 解析端点"


def test_ensure_vllm_base_url_is_gone():
    import src.services.inference.vllm_endpoint as ve
    assert not hasattr(ve, "ensure_vllm_base_url")


def _hash(t: str) -> str:
    return bcrypt.hashpw(t.encode(), bcrypt.gensalt()).decode()


async def _model_svc(db_session, name, engine, category="llm"):
    s = ServiceInstance(source_type="model", source_name=engine, name=name,
                        type="llm", status="active", category=category)
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


async def _grant_key(db_session, *services):
    raw = "sk-ready1234abcdef"
    k = InstanceApiKey(instance_id=None, label="t", key_hash=_hash(raw),
                       key_prefix=raw[:10], is_active=True)
    db_session.add(k)
    await db_session.commit()
    await db_session.refresh(k)
    db_session.add_all([ApiKeyGrant(api_key_id=k.id, service_id=s.id, status="active") for s in services])
    await db_session.commit()
    return raw


@pytest.mark.asyncio
async def test_chat_on_cold_model_is_503_model_not_ready_and_never_loads(db_client, db_session):
    cold = await _model_svc(db_session, "qwen3-8-27b", "qwen3_8_27b_abliterated_awq")
    hot = await _model_svc(db_session, "moss-asr", "moss_transcribe_diarize", category="asr")
    raw = await _grant_key(db_session, cold, hot)
    mgr = db_client._transport.app.state.model_manager
    mgr.get_adapter = MagicMock(return_value=None)                       # 冷:没有 adapter
    mgr.is_loaded = MagicMock(side_effect=lambda n: n == "moss_transcribe_diarize")

    r = await db_client.post(
        "/v1/chat/completions",
        json={"model": "qwen3-8-27b", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 503, r.text
    err = r.json()["error"]
    assert err["type"] == "model_not_ready" and err["code"] == "model_not_ready"
    assert err["param"] == "model"
    assert err["ready_models"] == ["moss-asr"]
    mgr.load_model.assert_not_called()   # 数据面绝不加载


@pytest.mark.asyncio
async def test_streaming_chat_on_cold_model_is_plain_503(db_client, db_session):
    cold = await _model_svc(db_session, "qwen3-8-27b", "qwen3_8_27b_abliterated_awq")
    raw = await _grant_key(db_session, cold)
    mgr = db_client._transport.app.state.model_manager
    mgr.get_adapter = MagicMock(return_value=None)
    r = await db_client.post(
        "/v1/chat/completions",
        json={"model": "qwen3-8-27b", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/json")   # 开流前拒绝,不是断掉的 SSE
    mgr.load_model.assert_not_called()


@pytest.mark.asyncio
async def test_v1_models_lists_only_ready_model_services(db_client, db_session):
    cold = await _model_svc(db_session, "qwen3-8-27b", "qwen3_8_27b_abliterated_awq")
    hot = await _model_svc(db_session, "moss-asr", "moss_transcribe_diarize", category="asr")
    wf = ServiceInstance(source_type="workflow", source_name="x", name="krea2",
                         type="inference", status="active", category="image")
    db_session.add(wf)
    await db_session.commit()
    await db_session.refresh(wf)
    raw = await _grant_key(db_session, cold, hot, wf)
    mgr = db_client._transport.app.state.model_manager
    mgr.is_loaded = MagicMock(side_effect=lambda n: n == "moss_transcribe_diarize")

    r = await db_client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text
    data = {m["id"]: m for m in r.json()["data"]}
    assert set(data) == {"moss-asr", "krea2"}           # 冷的 model 服务不出现;workflow 服务照旧
    assert all(m["owned_by"] == "nous-engine" for m in data.values())

    one = await db_client.get("/v1/models/qwen3-8-27b", headers={"Authorization": f"Bearer {raw}"})
    assert one.status_code == 503 and one.json()["error"]["code"] == "model_not_ready"
    missing = await db_client.get("/v1/models/nope", headers={"Authorization": f"Bearer {raw}"})
    assert missing.status_code == 404


def _wav16k(seconds: float = 0.5) -> bytes:
    """最小合法 16k/mono/s16le WAV —— multipart 需要个文件体,内容不重要
    (拒绝发生在 `file.read()` 之前)。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_transcriptions_on_cold_moss_is_503_model_not_ready(
    db_client, db_session, monkeypatch,
):
    """/v1/audio/transcriptions 的冷 MOSS 拒绝与 chat/embeddings 同一信封。

    修前是裸 `HTTPException(503, detail=...)` → main.py 的 503 没有映射条目,落
    `type=api_error` / `code=null`,下游既拿不到 code 也拿不到指向 /v1/models 的 fix。
    """
    moss = await _model_svc(db_session, "moss-asr", "moss_transcribe_diarize", category="asr")
    raw = await _grant_key(db_session, moss)
    # env override 会短路选址(直接返回 URL,根本不问 ModelManager)——必须清掉。
    monkeypatch.delenv("NOUS_MOSS_ASR_URL", raising=False)
    mgr = db_client._transport.app.state.model_manager
    mgr.get_adapter = MagicMock(return_value=None)          # 冷:没有 adapter

    r = await db_client.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {raw}"},
        files={"file": ("a.wav", _wav16k(), "audio/wav")},
        data={"model": "moss-asr"},
    )
    assert r.status_code == 503, r.text
    err = r.json()["error"]
    assert err["type"] == "model_not_ready" and err["code"] == "model_not_ready"
    assert err["param"] == "model"
    assert "/v1/models" in err["fix"]
    mgr.load_model.assert_not_called()


@pytest.mark.parametrize("path,body", [
    ("/v1/responses", {"model": "qwen3.5", "input": "hi"}),
    ("/v1/context/create",
     {"model": "qwen3.5", "messages": [{"role": "system", "content": "hi"}]}),
])
@pytest.mark.asyncio
async def test_cold_model_503_envelope_on_responses_and_context_cache(
    api_client, bearer_headers, path, body,
):
    """spec §5:剩下两个兼容面的 503 也要是 model_not_ready 信封(2026-09-05 复审 E8)。

    信封退化成 `type=api_error` / `code=null` 不会有任何测试发现,而下游正是按 code 分流的。
    """
    mgr = api_client.app.state.model_manager
    mgr.get_adapter = MagicMock(return_value=None)          # 冷:没有 adapter

    r = await api_client.post(path, json=body, headers=bearer_headers)
    assert r.status_code == 503, r.text
    err = r.json()["error"]
    assert err["type"] == "model_not_ready" and err["code"] == "model_not_ready"
    assert "/v1/models" in err["fix"]
    mgr.load_model.assert_not_called()
