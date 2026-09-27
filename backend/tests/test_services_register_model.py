"""v3 建模型服务端点 POST /api/v1/services/register-model —— 取代 legacy
POST /api/v1/instances(source_type=model),双轨收敛(#3)。ModelsOverlay「发布模型
为 API 接入点」流程改指向它。"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_register_model_creates_model_backed_service(db_client):
    r = await db_client.post(
        "/api/v1/services/register-model",
        json={"name": "nous-my-embed", "source_name": "qwen3_embedding_8b", "type": "embedding"},
    )
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["source_type"] == "model"
    assert b["source_name"] == "qwen3_embedding_8b"
    assert b["name"] == "nous-my-embed"
    # category 由引擎 type 派生(前端按此给对的调用端点)
    assert b["category"] == "embedding"


@pytest.mark.asyncio
async def test_register_model_rejects_duplicate_name(db_client):
    await db_client.post(
        "/api/v1/services/register-model",
        json={"name": "nous-dup-svc", "source_name": "qwen3_embedding_8b", "type": "embedding"},
    )
    r = await db_client.post(
        "/api/v1/services/register-model",
        json={"name": "nous-dup-svc", "source_name": "qwen3_asr", "type": "asr"},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_register_model_bad_name_422(db_client):
    r = await db_client.post(
        "/api/v1/services/register-model",
        json={"name": "Bad Name", "source_name": "x", "type": "llm"},
    )
    # 项目全局 handler 把 pydantic 校验错误统一转 400(与 quick-provision 同约定)。
    assert r.status_code == 400
