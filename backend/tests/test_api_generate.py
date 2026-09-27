from unittest.mock import patch


async def test_generate_video_returns_task_id(client):
    with patch("src.api.routes.generate.dispatch_task") as mock:
        mock.return_value = "fake-task-id"
        resp = await client.post(
            "/api/v1/generate/video",
            json={"prompt": "sunset timelapse"},
        )
        assert resp.status_code == 202
