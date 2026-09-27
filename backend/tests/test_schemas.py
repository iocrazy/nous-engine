from src.models.schemas import (
    TTSRequest,
    VideoGenerateRequest,
    ImageUnderstandRequest,
    TaskResponse,
    TaskStatus,
)


def test_tts_request():
    req = TTSRequest(text="Hello world", engine="cosyvoice2")
    assert req.engine == "cosyvoice2"


def test_tts_request_invalid_engine():
    import pytest
    with pytest.raises(ValueError):
        TTSRequest(text="Hello", engine="invalid")


def test_video_request():
    req = VideoGenerateRequest(prompt="a sunset timelapse")
    assert req.num_frames == 81


def test_image_understand_request():
    req = ImageUnderstandRequest(image_url="/path/to/img.png", question="What is this?")
    assert req.question == "What is this?"


def test_task_response():
    from src.utils.snowflake import snowflake_id
    tid = snowflake_id()
    resp = TaskResponse(
        id=tid,
        task_type="image",
        status=TaskStatus.PENDING,
    )
    assert resp.id == tid
    assert resp.status == "pending"
