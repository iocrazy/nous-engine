from unittest.mock import patch

import httpx
import pytest
from fastapi import Response
from starlette.requests import Request

from src.config import Settings
from src.api.routes import skill_runs


@pytest.fixture
def skill_home(tmp_path):
    settings = Settings(NOUS_CENTER_HOME=str(tmp_path))
    with patch("src.services.skill_manager.get_settings", return_value=settings):
        yield tmp_path


@pytest.mark.asyncio
async def test_preview_inline_skill(api_client, bearer_headers, mock_vllm):
    response = await api_client.post(
        "/v1/skill-runs/preview",
        headers=bearer_headers,
        json={
            "model": "qwen3.5",
            "instruction": "Create a cinematic portrait",
            "skill": {
                "name": "cinematic",
                "description": "Expand a short image request",
                "body": "Preserve the subject and add camera and lighting details.",
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "prompt": "ok",
        "model": "qwen3.5",
        "skill": {"name": "cinematic", "source": "inline"},
    }


@pytest.mark.asyncio
async def test_preview_stored_skill(api_client, bearer_headers, skill_home, mock_vllm):
    skill_dir = skill_home / "skills" / "portrait"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: portrait\ndescription: Portrait prompt\n---\n\nAdd lens details.",
        encoding="utf-8",
    )

    response = await api_client.post(
        "/v1/skill-runs/preview",
        headers=bearer_headers,
        json={
            "model": "qwen3.5",
            "instruction": "A person by a window",
            "skill_name": "portrait",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["skill"] == {"name": "portrait", "source": "nous-engine"}


@pytest.mark.asyncio
async def test_preview_requires_one_skill_source(api_client, bearer_headers):
    response = await api_client.post(
        "/v1/skill-runs/preview",
        headers=bearer_headers,
        json={"model": "qwen3.5", "instruction": "A portrait"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_preview_requires_auth(api_client):
    response = await api_client.post(
        "/v1/skill-runs/preview",
        json={
            "model": "qwen3.5",
            "instruction": "A portrait",
            "skill": {"body": "Expand the prompt."},
        },
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_generate_injects_confirmed_prompt(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            pass

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(
                202,
                json={"id": "42", "status": "processing"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(skill_runs.httpx, "AsyncClient", FakeClient)
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/v1/skill-runs/generate",
        "headers": [(b"authorization", b"Bearer test-key")],
        "app": object(),
    })
    response = Response()
    body = skill_runs.GenerateRequest.model_validate({
        "prompt": "confirmed prompt",
        "generation": {
            "service": "studio-text-to-image",
            "input": {"width": 1024, "height": 1024},
            "prompt_field": "positive_prompt",
        },
    })

    result = await skill_runs.generate_from_skill(
        body, request, response, prefer="respond-async", _auth=(None, object()),
    )

    assert response.status_code == 202
    assert result["prompt"] == "confirmed prompt"
    assert result["prediction"]["status"] == "processing"
    assert calls == [(
        "/v1/services/studio-text-to-image/predictions",
        {
            "json": {"input": {
                "width": 1024,
                "height": 1024,
                "positive_prompt": "confirmed prompt",
            }},
            "headers": {
                "Authorization": "Bearer test-key",
                "Prefer": "respond-async",
            },
        },
    )]
