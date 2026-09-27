"""POST /v1/skill-runs/generate(spec 2026-09-26 skill-runs §3.2)。

刻意用一个与任何真实模板无关的**通用假工作流服务**,文本字段叫 `caption` 而不是 `prompt`
—— 证明编排只认「服务名 + 输入 key」。执行器打桩,只记录收到的快照。
"""
from __future__ import annotations

import bcrypt
import pytest

from src.models.api_gateway import ApiKeyGrant
from src.models.execution_task import ExecutionTask
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance

SNAPSHOT = {"nodes": {
    "n1": {"class_type": "FakeText", "inputs": {"text": ""}},
    "n2": {"class_type": "FakeLoader", "inputs": {"file": ""}},
    "n3": {"class_type": "FakeSampler", "inputs": {"steps": 20, "style": "a"}},
}}
EXPOSED = [
    {"key": "caption", "node_id": "n1", "input_name": "text", "type": "string", "required": True},
    {"key": "ref", "node_id": "n2", "input_name": "file", "type": "image", "required": False},
    {"key": "style", "node_id": "n3", "input_name": "style", "type": "string", "required": False,
     "constraints": {"enum": ["a", "b"]}},
    {"key": "steps", "node_id": "n3", "input_name": "steps", "type": "integer", "required": False},
]


@pytest.fixture
def captured_runs(monkeypatch):
    runs: list[dict] = []

    async def _fake_run(task_id, snapshot, **_kw):
        runs.append(snapshot)

    import src.services.workflow_runner as workflow_runner
    monkeypatch.setattr(workflow_runner, "run_workflow_task", _fake_run)
    return runs


async def _service(db_session, name="any-captioner", source_type="workflow"):
    svc = ServiceInstance(
        name=name, type="inference", status="active", source_type=source_type,
        source_id=1, source_name=name if source_type == "model" else None,
        category="video", workflow_snapshot=SNAPSHOT, exposed_inputs=EXPOSED, exposed_outputs=[],
    )
    db_session.add(svc)
    await db_session.commit()
    await db_session.refresh(svc)
    return svc


def _req(**over):
    body = {"service": "any-captioner", "prompt_field": "caption",
            "text": "a slow dolly-in on a lighthouse at dusk", "input": {"steps": 8}}
    body.update(over)
    return body


def _node(snapshot: dict, node_id: str) -> dict:
    return next(n for n in snapshot["nodes"] if n["id"] == node_id)


@pytest.mark.asyncio
async def test_generate_injects_text_into_prompt_field(db_client, db_session, captured_runs):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate", json=_req())
    assert r.status_code == 200, r.text
    pred = r.json()
    assert pred["service"] == "any-captioner"
    assert pred["input"] == {"steps": 8, "caption": "a slow dolly-in on a lighthouse at dusk"}
    snap = captured_runs[0]
    assert _node(snap, "n1")["data"]["text"] == "a slow dolly-in on a lighthouse at dusk"
    assert _node(snap, "n3")["data"]["steps"] == 8
    task = await db_session.get(ExecutionTask, int(pred["id"]))
    assert task.input_json["caption"] == "a slow dolly-in on a lighthouse at dusk"


@pytest.mark.asyncio
async def test_generate_respond_async_is_202(db_client, db_session, captured_runs):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate", json=_req(),
                             headers={"Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] in ("starting", "processing")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["ref", "style", "steps", "nope"])
async def test_generate_rejects_non_text_prompt_field(db_client, db_session, captured_runs, field):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate", json=_req(prompt_field=field))
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "invalid_prompt_field"
    assert err["text_fields"] == ["caption"]
    assert captured_runs == []


@pytest.mark.asyncio
async def test_generate_prompt_field_conflict(db_client, db_session, captured_runs):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate",
                             json=_req(input={"caption": "from input"}))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "prompt_field_conflict"


@pytest.mark.asyncio
async def test_generate_empty_text(db_client, db_session, captured_runs):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate", json=_req(text="  "))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "empty_text"


@pytest.mark.asyncio
async def test_generate_text_size_cap(db_client, db_session, captured_runs):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate", json=_req(text="x" * 100_001))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "validation_error"
    assert captured_runs == []


@pytest.mark.asyncio
async def test_generate_other_inputs_still_schema_validated(db_client, db_session, captured_runs):
    await _service(db_session)
    r = await db_client.post("/v1/skill-runs/generate", json=_req(input={"steps": "many"}))
    assert r.status_code == 422, r.text          # predictions 既有的 schema 校验照常生效
    assert captured_runs == []


@pytest.mark.asyncio
async def test_generate_on_model_service_is_400(db_client, db_session, captured_runs):
    await _service(db_session, name="some-llm", source_type="model")
    r = await db_client.post("/v1/skill-runs/generate", json=_req(service="some-llm"))
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_generate_unknown_service_is_404(db_client, db_session, captured_runs):
    r = await db_client.post("/v1/skill-runs/generate", json=_req(service="nope"))
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_generate_bearer_key_needs_grant(db_client, db_session, captured_runs):
    svc = await _service(db_session)
    await _service(db_session, name="not-granted")
    raw = "sk-gen1234abcdef00"
    key = InstanceApiKey(instance_id=None, label="t", is_active=True, key_prefix=raw[:10],
                         key_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode())
    db_session.add(key)
    await db_session.commit()
    await db_session.refresh(key)
    db_session.add(ApiKeyGrant(api_key_id=key.id, service_id=svc.id, status="active"))
    await db_session.commit()
    headers = {"Authorization": f"Bearer {raw}"}

    ok = await db_client.post("/v1/skill-runs/generate", json=_req(), headers=headers)
    assert ok.status_code == 200, ok.text
    task = await db_session.get(ExecutionTask, int(ok.json()["id"]))
    assert task.api_key_id == key.id              # 归属到 key(IDOR 防护依赖它)

    denied = await db_client.post("/v1/skill-runs/generate",
                                  json=_req(service="not-granted"), headers=headers)
    assert denied.status_code == 404, denied.text
