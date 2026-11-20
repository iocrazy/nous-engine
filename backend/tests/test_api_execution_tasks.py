from httpx import AsyncClient

# 本文件统一走 pytest-asyncio(pyproject 里 asyncio_mode=auto,async 测试自动挂 mark,
# 不需要显式 pytestmark —— 模块级 mark 会连同步测试一起标上,pytest-asyncio 会为每个
# 同步测试报一条 "is marked with '@pytest.mark.asyncio' but it is not an async function")。
# 别改用 anyio:混用会各建一个事件循环,asyncpg 的连接绑 loop 就抛 "attached to a
# different loop"(aiosqlite 不在意,所以以前看不出来)。

async def test_list_tasks_empty(db_client: AsyncClient):
    resp = await db_client.get("/api/v1/tasks")
    assert resp.status_code == 200
    assert resp.json() == []

async def test_record_task(db_client: AsyncClient):
    resp = await db_client.post("/api/v1/tasks/record", json={
        "workflow_name": "test_workflow",
        "status": "completed",
        "nodes_total": 3,
        "nodes_done": 3,
        "duration_ms": 1234,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["workflow_name"] == "test_workflow"
    assert data["status"] == "completed"
    assert data["duration_ms"] == 1234

async def test_record_task_invalid_status(db_client: AsyncClient):
    resp = await db_client.post("/api/v1/tasks/record", json={
        "workflow_name": "test",
        "status": "INVALID",
    })
    assert resp.status_code == 400

async def test_get_task(db_client: AsyncClient):
    # Create a task first
    resp = await db_client.post("/api/v1/tasks/record", json={
        "workflow_name": "get_test",
        "status": "completed",
        "nodes_total": 1,
        "nodes_done": 1,
    })
    task_id = resp.json()["id"]

    # Get it
    resp = await db_client.get(f"/api/v1/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["workflow_name"] == "get_test"

async def test_get_task_not_found(db_client: AsyncClient):
    resp = await db_client.get("/api/v1/tasks/999999999")
    assert resp.status_code == 404

async def test_delete_task(db_client: AsyncClient):
    resp = await db_client.post("/api/v1/tasks/record", json={
        "workflow_name": "delete_test",
        "status": "failed",
    })
    task_id = resp.json()["id"]

    resp = await db_client.delete(f"/api/v1/tasks/{task_id}")
    assert resp.status_code == 200

    resp = await db_client.get(f"/api/v1/tasks/{task_id}")
    assert resp.status_code == 404

async def test_cancel_task(db_client: AsyncClient):
    resp = await db_client.post("/api/v1/tasks/record", json={
        "workflow_name": "cancel_test",
        "status": "running",
    })
    task_id = resp.json()["id"]

    resp = await db_client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 200

    resp = await db_client.get(f"/api/v1/tasks/{task_id}")
    assert resp.json()["status"] == "cancelled"

async def test_cancel_completed_task_fails(db_client: AsyncClient):
    resp = await db_client.post("/api/v1/tasks/record", json={
        "workflow_name": "cancel_fail",
        "status": "completed",
    })
    task_id = resp.json()["id"]

    resp = await db_client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 400

async def test_list_tasks_with_filter(db_client: AsyncClient):
    await db_client.post("/api/v1/tasks/record", json={
        "workflow_name": "filter1", "status": "completed",
    })
    await db_client.post("/api/v1/tasks/record", json={
        "workflow_name": "filter2", "status": "failed",
    })

    resp = await db_client.get("/api/v1/tasks?status=failed")
    tasks = resp.json()
    assert all(t["status"] == "failed" for t in tasks)


async def test_list_tasks_workflow_id_filter(db_client: AsyncClient, db_session):
    # 服务用量/历史 tab 按 workflow_id 归属过滤(spec run-history PR-C)。
    from src.models.execution_task import ExecutionTask

    db_session.add_all([
        ExecutionTask(workflow_name="svc-a", workflow_id=111, status="completed", nodes_total=1),
        ExecutionTask(workflow_name="svc-b", workflow_id=222, status="completed", nodes_total=1),
    ])
    await db_session.commit()

    resp = await db_client.get("/api/v1/tasks?workflow_id=111")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["workflow_id"] for r in rows] == ["111"]  # 序列化成 str

    # 非法 workflow_id → 空集,不抛错
    bad = await db_client.get("/api/v1/tasks?workflow_id=notanumber")
    assert bad.status_code == 200 and bad.json() == []


def test_detect_image_meta_recognizes_image_output_envelope():
    from src.api.routes.execution_tasks import _detect_image_meta

    result = {
        "node-abc": {"text": "hi"},
        "node-img": {
            "image_url": "/v1/images/x.png?sig=...",
            "media_type": "image/png",
            "width": 1024,
            "height": 1024,
        },
    }
    meta = _detect_image_meta(result)
    assert meta == {"task_type": "image", "image_width": 1024, "image_height": 1024}


def test_detect_image_meta_returns_none_for_text_only_result():
    from src.api.routes.execution_tasks import _detect_image_meta

    assert _detect_image_meta({"a": {"text": "hello"}}) == {
        "task_type": None,
        "image_width": None,
        "image_height": None,
    }
    assert _detect_image_meta(None) == {
        "task_type": None,
        "image_width": None,
        "image_height": None,
    }


def test_detect_image_meta_handles_image_without_dimensions():
    from src.api.routes.execution_tasks import _detect_image_meta

    meta = _detect_image_meta({"out": {"media_type": "image/jpeg"}})
    assert meta["task_type"] == "image"
    assert meta["image_width"] is None
    assert meta["image_height"] is None


def test_detect_vision_meta_recognizes_multimodal_marker():
    """PR-1d:LLMNode 标了 multimodal=True → _detect_vision_meta 归 type=vision。"""
    from src.api.routes.execution_tasks import _detect_vision_meta

    result = {
        "vis-1": {
            "text": "a cat",
            "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
            "multimodal": True,
        }
    }
    meta = _detect_vision_meta(result)
    assert meta == {"task_type": "vision", "vision_completion_tokens": 8}


def test_detect_vision_meta_returns_none_for_pure_llm():
    """multimodal=False 或缺失 → 不算 vision(由 _detect_llm_meta 兜底)。"""
    from src.api.routes.execution_tasks import _detect_vision_meta

    assert _detect_vision_meta({"a": {"text": "x", "usage": {}}})["task_type"] is None
    assert _detect_vision_meta(
        {"a": {"text": "x", "usage": {}, "multimodal": False}})["task_type"] is None


def test_task_to_dict_vision_takes_precedence_over_llm():
    """PR-1d 关键:vision 检测顺序在 LLM 之前 —— 同样 {text, usage} envelope,有
    multimodal=True 就归 vision,无就归 llm。"""
    from datetime import datetime, timezone

    from src.api.routes.execution_tasks import _task_to_dict
    from src.models.execution_task import ExecutionTask

    now = datetime.now(timezone.utc)
    t_vision = ExecutionTask(
        id=6, workflow_id=1, workflow_name="vqa", status="completed",
        nodes_total=2, nodes_done=2, current_node=None,
        result={"vis-1": {
            "text": "a cat",
            "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
            "multimodal": True,
        }},
        error=None, duration_ms=900, created_at=now, updated_at=now,
    )
    d = _task_to_dict(t_vision)
    assert d["type"] == "vision"
    assert d["vision_completion_tokens"] == 8
    # 不应该被误标为 llm
    assert d.get("llm_completion_tokens") is None


def test_detect_llm_meta_recognizes_text_and_usage_envelope():
    """PR-1c:LLM workflow result envelope 识别(text + usage)。"""
    from src.api.routes.execution_tasks import _detect_llm_meta

    result = {
        "llm-node": {
            "text": "answer",
            "usage": {"prompt_tokens": 12, "completion_tokens": 47, "total_tokens": 59},
            "duration_ms": 1234,
        }
    }
    meta = _detect_llm_meta(result)
    assert meta == {
        "task_type": "llm",
        "llm_prompt_tokens": 12,
        "llm_completion_tokens": 47,
    }


def test_detect_llm_meta_returns_none_for_non_llm():
    """text 但无 usage / usage 但无 text → 不算 LLM。"""
    from src.api.routes.execution_tasks import _detect_llm_meta

    # 只有 text(text_input 节点)
    assert _detect_llm_meta({"a": {"text": "hi"}})["task_type"] is None
    # 只有 usage(理论上不会发生但保护)
    assert _detect_llm_meta({"a": {"usage": {}}})["task_type"] is None


def test_task_to_dict_exposes_type_field_for_llm_result():
    """PR-1c:LLM-only result → type=\"llm\" + llm_prompt/completion_tokens。"""
    from datetime import datetime, timezone

    from src.api.routes.execution_tasks import _task_to_dict
    from src.models.execution_task import ExecutionTask

    now = datetime.now(timezone.utc)
    t_llm = ExecutionTask(
        id=5, workflow_id=1, workflow_name="chat", status="completed",
        nodes_total=2, nodes_done=2, current_node=None,
        result={"llm-1": {
            "text": "hi",
            "usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
            "duration_ms": 500,
        }},
        error=None, duration_ms=500, created_at=now, updated_at=now,
    )
    d = _task_to_dict(t_llm)
    assert d["type"] == "llm"
    assert d["task_type"] == "llm"
    assert d["llm_prompt_tokens"] == 5
    assert d["llm_completion_tokens"] == 10


def test_detect_tts_meta_recognizes_audio_envelope():
    """PR-1b:TTS workflow result envelope 识别(audio_url + duration_seconds)。"""
    from src.api.routes.execution_tasks import _detect_tts_meta

    result = {
        "node-tts": {
            "audio_url": "/v1/audio/x.wav?sig=...",
            "media_type": "audio/wav",
            "duration_seconds": 3.42,
        }
    }
    meta = _detect_tts_meta(result)
    assert meta == {"task_type": "tts", "audio_duration_seconds": 3.42}


def test_detect_tts_meta_via_meta_envelope():
    """PR-1b:duration 可能在 meta 子对象里(runner_process outputs envelope 形状)。"""
    from src.api.routes.execution_tasks import _detect_tts_meta

    result = {
        "node-tts": {
            "media_type": "audio/wav",
            "meta": {"duration_seconds": 5.0, "sample_rate": 24000},
        }
    }
    meta = _detect_tts_meta(result)
    assert meta["task_type"] == "tts"
    assert meta["audio_duration_seconds"] == 5.0


def test_detect_tts_meta_returns_none_for_image_result():
    """PR-1b:image result(audio media_type 缺)→ TTS 检测 None。"""
    from src.api.routes.execution_tasks import _detect_tts_meta

    assert _detect_tts_meta({"x": {"media_type": "image/png"}})["task_type"] is None


def test_task_to_dict_exposes_type_field_for_tts_result():
    """PR-1b:TTS-only result → _task_to_dict 加 type=\"tts\" + audio_duration_seconds。"""
    from datetime import datetime, timezone

    from src.api.routes.execution_tasks import _task_to_dict
    from src.models.execution_task import ExecutionTask

    now = datetime.now(timezone.utc)
    t_tts = ExecutionTask(
        id=4, workflow_id=1, workflow_name="w", status="completed",
        nodes_total=1, nodes_done=1, current_node=None,
        result={"node-tts": {
            "audio_url": "/v1/audio/x.wav",
            "media_type": "audio/wav",
            "duration_seconds": 2.5,
        }},
        error=None, duration_ms=200, created_at=now, updated_at=now,
    )
    d = _task_to_dict(t_tts)
    assert d["type"] == "tts"
    assert d["task_type"] == "tts"
    assert d["audio_duration_seconds"] == 2.5


def test_detect_asr_meta_recognizes_flat_envelope():
    """Arc 3(spec 2026-07-20-moss-asr §8):ASR 直连 task result 是 flat 顶层
    {text, segments_count, speakers, audio_seconds}(非 workflow 节点输出);
    audio_seconds 是判据。"""
    from src.api.routes.execution_tasks import _detect_asr_meta

    result = {"text": "你好", "segments_count": 3, "speakers": ["S01"], "audio_seconds": 12}
    meta = _detect_asr_meta(result)
    assert meta == {"task_type": "asr", "audio_seconds": 12, "segments_count": 3}


def test_detect_asr_meta_returns_none_without_audio_seconds():
    """无 audio_seconds 判据(workflow 结果 / 其它 result)→ 不误判 asr。"""
    from src.api.routes.execution_tasks import _detect_asr_meta

    assert _detect_asr_meta({"outputs": {"n": {"text": "hi"}}})["task_type"] is None
    assert _detect_asr_meta({"text": "hi"})["task_type"] is None
    assert _detect_asr_meta(None)["task_type"] is None


def test_task_to_dict_exposes_type_field_for_asr_result():
    """Arc 3:ASR-only flat result → type=\"asr\" + audio_seconds + segments_count。"""
    from datetime import datetime, timezone

    from src.api.routes.execution_tasks import _task_to_dict
    from src.models.execution_task import ExecutionTask

    now = datetime.now(timezone.utc)
    t_asr = ExecutionTask(
        id=9, workflow_id=None, workflow_name="moss-asr", status="completed",
        nodes_total=0, nodes_done=0, current_node=None,
        result={"text": "你好世界", "segments_count": 2,
                "speakers": ["S01", "S02"], "audio_seconds": 7},
        error=None, duration_ms=800, created_at=now, updated_at=now,
    )
    d = _task_to_dict(t_asr)
    assert d["type"] == "asr"
    assert d["task_type"] == "asr"
    assert d["audio_seconds"] == 7
    assert d["segments_count"] == 2


def test_task_to_dict_exposes_type_field_for_image_result():
    """PR-1a:_task_to_dict 加显式 `type` 字段(对齐 spec State model ServiceType)。
    image result → type="image";text-only / None result → type=None(前端 Other 兜底)。
    沿用 _detect_image_meta 的检测,tts/llm/vision 由后续 PR-1b/c/d 扩展。"""
    from datetime import datetime, timezone

    from src.api.routes.execution_tasks import _task_to_dict
    from src.models.execution_task import ExecutionTask

    now = datetime.now(timezone.utc)
    # image result
    t_img = ExecutionTask(
        id=1, workflow_id=1, workflow_name="w", status="completed",
        nodes_total=1, nodes_done=1, current_node=None,
        result={"out": {"image_url": "/x.png", "media_type": "image/png",
                        "width": 1024, "height": 1024}},
        error=None, duration_ms=100, created_at=now, updated_at=now,
    )
    d = _task_to_dict(t_img)
    assert d["type"] == "image"
    assert d["task_type"] == "image"  # 旧字段保留(向后兼容)

    # text-only result → type=None
    t_text = ExecutionTask(
        id=2, workflow_id=1, workflow_name="w2", status="completed",
        nodes_total=1, nodes_done=1, current_node=None,
        result={"out": {"text": "hi"}},
        error=None, duration_ms=50, created_at=now, updated_at=now,
    )
    assert _task_to_dict(t_text)["type"] is None

    # None result → type=None
    t_none = ExecutionTask(
        id=3, workflow_id=1, workflow_name="w3", status="queued",
        nodes_total=0, nodes_done=0, current_node=None,
        result=None, error=None, duration_ms=None,
        created_at=now, updated_at=now,
    )
    assert _task_to_dict(t_none)["type"] is None


def test_task_to_dict_output_thumbnails():
    # 出图缩略抽取(spec run-history PR-B):result.outputs[*].image_url → output_thumbnails。
    from datetime import datetime, timezone

    from src.api.routes.execution_tasks import _task_to_dict
    from src.models.execution_task import ExecutionTask

    now = datetime.now(timezone.utc)
    t_img = ExecutionTask(
        id=21, workflow_id=1, workflow_name="img", status="completed",
        nodes_total=1, nodes_done=1, current_node=None,
        result={"outputs": {"dec": {"image_url": "/files/a.png?t=1", "media_type": "image/png"}}},
        error=None, duration_ms=100, created_at=now, updated_at=now,
    )
    d = _task_to_dict(t_img)
    assert d["output_thumbnails"] == ["/files/a.png?t=1"]
    assert d["type"] == "image"

    t_txt = ExecutionTask(
        id=22, workflow_id=1, workflow_name="llm", status="completed",
        nodes_total=1, nodes_done=1, current_node=None,
        result={"outputs": {"o": {"text": "hi"}}},
        error=None, duration_ms=5, created_at=now, updated_at=now,
    )
    assert _task_to_dict(t_txt)["output_thumbnails"] == []


async def test_collect_referenced_image_uuids(db_client: AsyncClient, db_session):
    # reaper 保留集:从 ExecutionTask.result 抽 image_url 的 uuid(spec run-history 图寿命=任务寿命)。
    from src.api.routes.execution_tasks import collect_referenced_image_uuids
    from src.models.execution_task import ExecutionTask

    db_session.add(ExecutionTask(
        workflow_name="img", workflow_id=1, status="completed", nodes_total=1,
        result={"outputs": {"dec": {"image_url": "/files/images/2026-06-10/abc123def.png?token=t&expires=1"}}},
    ))
    db_session.add(ExecutionTask(
        workflow_name="llm", workflow_id=2, status="completed", nodes_total=1,
        result={"outputs": {"o": {"text": "no image here"}}},
    ))
    await db_session.commit()

    uuids = await collect_referenced_image_uuids(db_session)
    assert "abc123def" in uuids


async def test_gallery_pages_skip_non_media_and_load_full_parameters_on_demand(db_client, db_session):
    from base64 import b64encode
    from io import BytesIO

    from PIL import Image

    from src.models.execution_task import ExecutionTask

    png = BytesIO()
    Image.new("RGB", (768, 384), "navy").save(png, format="PNG")
    inline = "data:image/png;base64," + b64encode(png.getvalue()).decode()
    file_url = "/files/images/2026-01-01/image.png"
    tasks = [
        ExecutionTask(id=101, status="completed", result={"node": {"image_url": file_url}}),
        ExecutionTask(id=102, status="completed", result={"node": {"text": "another task"}}),
        ExecutionTask(
            id=103,
            status="completed",
            result={"outputs": {"node": {"image_url": inline}}},
            input_json={"image": inline, "prompt": "paint the sky", "seed": 4},
        ),
        ExecutionTask(id=104, status="failed", result={"node": {"image_url": file_url}}),
    ]
    db_session.add_all(tasks)
    await db_session.commit()

    response = await db_client.get("/api/v1/tasks/gallery?limit=1")
    assert response.status_code == 200
    page = response.json()
    assert page["next_before"] == "103"
    assert [item["id"] for item in page["items"]] == ["103"]
    assert len(response.content) < 512
    image_url = page["items"][0]["output_thumbnails"][0]
    assert image_url == "/api/v1/tasks/gallery/103/image/0"

    older = (await db_client.get("/api/v1/tasks/gallery?limit=1&before=103")).json()
    assert [item["id"] for item in older["items"]] == ["101"]
    assert older["next_before"] is None

    preview = await db_client.get(image_url, params={"preview": 1})
    assert preview.headers["content-type"] == "image/webp"
    assert Image.open(BytesIO(preview.content)).size == (384, 192)
    assert (await db_client.get(image_url)).content == png.getvalue()
    metadata = await db_client.get("/api/v1/tasks/gallery/103/metadata")
    assert metadata.json() == {"prompt": "paint the sky", "fields": [{"label": "seed", "value": "4"}], "tags": []}
    summary = await db_client.get("/api/v1/tasks?include_input=false")
    assert all("input_json" not in item for item in summary.json())
    assert (await db_client.get("/api/v1/tasks/103")).json()["input_json"]["image"] == inline


async def test_gallery_filters_searches_past_nonmatching_tasks_and_reports_expiry(db_client, db_session, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from src.models.execution_task import ExecutionTask

    monkeypatch.setenv("NOUS_USAGE_RETENTION_DAYS", "14")
    now = datetime.now(timezone.utc)
    db_session.add_all([
        ExecutionTask(id=100, status="completed", workflow_name="Archive",
                      created_at=now - timedelta(days=15), result={"n": {"video_url": "/old.mp4"}}),
        ExecutionTask(id=101, status="completed", workflow_name="Comfy Video",
                      created_at=now, input_json={"prompt": "Rain over a bridge"},
                      result={"n": {"video_url": "/new.mp4"}}),
        ExecutionTask(id=102, status="completed", workflow_name="Comfy Image",
                      created_at=now, input_json={"prompt": "Rain over a bridge"},
                      result={"n": {"image_url": "/new.png"}}),
    ])
    await db_session.commit()

    response = await db_client.get("/api/v1/tasks/gallery", params={"media": "video", "q": "BRIDGE", "days": 7})
    response.raise_for_status()
    [item] = response.json()["items"]
    assert item["id"] == "101" and item["kind"] == "video"
    assert datetime.fromisoformat(item["expires_at"]) == now + timedelta(days=14)
    for q in ("Comfy Image", "102"):
        by_name = await db_client.get("/api/v1/tasks/gallery", params={"q": q})
        assert [item["id"] for item in by_name.json()["items"]] == ["102"]


async def test_gallery_permanent_delete_keeps_shared_files_and_deletes_local_outputs(db_client, db_session, monkeypatch, tmp_path):
    from src.models.execution_task import ExecutionTask
    from src.services.image_output_storage import write_image

    monkeypatch.setenv("NOUS_IMAGE_OUTPUTS", str(tmp_path))
    own = write_image(b"own", ext="png")
    shared = write_image(b"shared", ext="mp4")
    poster = write_image(b"preview", ext="webp")
    missing = write_image(b"missing", ext="png")
    missing["path"].unlink()
    outside = tmp_path / "comfyui.mp4"
    outside.write_bytes(b"external")
    urls = {
        item["uuid"]: f"/files/images/{item['date']}/{item['uuid']}.{item['ext']}"
        for item in (own, shared, poster, missing)
    }
    db_session.add_all([
        ExecutionTask(id=51, status="completed", result={"n": {"image_url": urls[own["uuid"]]}}),
        ExecutionTask(id=52, status="completed", result={"n": {
            "video_url": urls[shared["uuid"]], "thumbnails": [urls[poster["uuid"]], urls[missing["uuid"]]],
            "items": [{"url": str(outside)}],
        }}),
        ExecutionTask(id=53, status="running", input_json={"image": urls[shared["uuid"]] + "?token=another"}),
    ])
    await db_session.commit()

    response = await db_client.post("/api/v1/tasks/gallery/delete", json={
        "task_ids": ["51", "52", "53", "99"], "permanent": True,
    })
    assert response.status_code == 200
    assert response.json() == {
        "deleted_ids": ["51", "52"], "skipped": 2, "removed_files": 2,
        "missing_files": 1, "shared_files": 1,
    }
    assert not own["path"].exists() and not poster["path"].exists()
    assert shared["path"].exists() and outside.exists()
    assert (await db_client.get("/api/v1/tasks/51")).status_code == 404
    assert (await db_client.get("/api/v1/tasks/53")).status_code == 200


async def test_gallery_bulk_delete_without_permanent_keeps_files_for_retention(db_client, db_session, monkeypatch, tmp_path):
    from src.models.execution_task import ExecutionTask
    from src.services.image_output_storage import write_image

    monkeypatch.setenv("NOUS_IMAGE_OUTPUTS", str(tmp_path))
    saved = write_image(b"original", ext="png")
    db_session.add(ExecutionTask(id=61, status="completed", result={
        "node": {"image_url": f"/files/images/{saved['date']}/{saved['uuid']}.png"},
    }))
    await db_session.commit()
    response = await db_client.post("/api/v1/tasks/gallery/delete", json={"task_ids": ["61"]})
    assert response.json()["deleted_ids"] == ["61"]
    assert response.json()["removed_files"] == 0
    assert saved["path"].exists()


async def test_gallery_permanent_delete_rejects_a_symlinked_storage_day(db_client, db_session, monkeypatch, tmp_path):
    from src.models.execution_task import ExecutionTask

    root = tmp_path / "outputs"
    root.mkdir()
    outside = tmp_path / "comfyui"
    outside.mkdir()
    basename = "a" * 32 + ".png"
    original = outside / basename
    original.write_bytes(b"comfy original")
    (root / "2026-01-01").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("NOUS_IMAGE_OUTPUTS", str(root))
    db_session.add(ExecutionTask(id=62, status="completed", result={
        "node": {"image_url": f"/files/images/2026-01-01/{basename}"},
    }))
    await db_session.commit()
    response = await db_client.post("/api/v1/tasks/gallery/delete", json={
        "task_ids": ["62"], "permanent": True,
    })
    assert response.status_code == 409
    assert original.read_bytes() == b"comfy original"
    assert (await db_client.get("/api/v1/tasks/62")).status_code == 200


async def test_gallery_tags_are_persisted_searchable_and_removable(db_client, db_session):
    from src.models.execution_task import ExecutionTask

    db_session.add(ExecutionTask(id=121, status="completed", workflow_name="image job", result={
        "node": {"image_url": "/new.png"},
    }))
    await db_session.commit()
    endpoint = "/api/v1/tasks/gallery/121/tags"
    saved = await db_client.put(endpoint, json={"tags": [" 参考图 ", "Pose", "pose"]})
    assert saved.json() == {"tags": ["参考图", "pose"]}
    details = await db_client.get("/api/v1/tasks/gallery/121/metadata")
    assert details.json()["tags"] == ["参考图", "pose"]
    gallery = await db_client.get("/api/v1/tasks/gallery", params={"q": "参考图"})
    assert [item["id"] for item in gallery.json()["items"]] == ["121"]
    assert (await db_client.put(endpoint, json={"tags": []})).json() == {"tags": []}
    assert (await db_client.get("/api/v1/tasks/gallery", params={"q": "参考图"})).json()["items"] == []
    assert (await db_client.put(endpoint, json={"tags": ["x" * 33]})).status_code == 400


async def test_gallery_model_order_and_exact_tag_filter(db_client, db_session):
    from src.models.execution_task import ExecutionTask

    db_session.add_all([
        ExecutionTask(id=141, status="completed", workflow_name="nous-qwen21-all-in-one",
                      gallery_tags=["穿搭"], input_json={"input_mode": "text", "seed": 42},
                      result={"node": {"image_url": "/a.png"}}),
        ExecutionTask(id=142, status="completed", workflow_name="nous-qwen21-all-in-one",
                      gallery_tags=["穿搭风"], input_json={"input_mode": "moss_text", "moss_steps": 25},
                      result={"node": {"image_url": "/b.png"}}),
        ExecutionTask(id=143, status="completed", workflow_name="nous-qwen21-all-in-one",
                      gallery_tags=["穿搭"], input_json={"input_mode": "reference", "reference_engine": "moss", "seed": 0},
                      result={"outputs": {"node": {"items": [{"url": "/c.png"}]}}}),
        ExecutionTask(id=144, status="completed", workflow_name="nonmedia", result={"node": {"text": "answer"}}),
    ])
    await db_session.commit()
    route = "/api/v1/tasks/gallery"
    models = (await db_client.get(route + "/models")).raise_for_status().json()
    assert models == ["MOSS Image", "Qwen Image 2.1"]
    first = (await db_client.get(route, params={"sort": "model", "limit": 1})).raise_for_status().json()
    assert first["next_model"] == "MOSS Image"
    assert first["items"][0]["id"] == "143"
    assert first["items"][0]["parameters"] == {"input_mode": "reference", "seed": "0"}
    second = (await db_client.get(route, params={
        "sort": "model", "before": first["next_before"], "before_model": first["next_model"],
    })).raise_for_status().json()
    assert [item["id"] for item in second["items"]] == ["142", "141"]
    exact = (await db_client.get(route, params={"tag": "穿搭", "sort": "oldest"})).raise_for_status().json()
    assert [item["id"] for item in exact["items"]] == ["141", "143"]
    moss = (await db_client.get(route, params={"tag": "穿搭", "model": "MOSS Image"})).raise_for_status().json()
    assert [item["id"] for item in moss["items"]] == ["143"]
