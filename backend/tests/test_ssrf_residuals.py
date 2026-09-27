"""二轮安全:SSRF 残留 —— chat messages image_url + responses input_image + verify_token 加固。"""
import pytest

from src.utils.url_security import UnsafeURLError, validate_chat_image_urls


class TestChatImageUrls:
    @pytest.mark.asyncio
    async def test_blocks_private_image_url(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "http://169.254.169.254/latest"}},
        ]}]
        with pytest.raises(UnsafeURLError):
            await validate_chat_image_urls(messages)

    @pytest.mark.asyncio
    async def test_allows_data_uri_and_public(self):
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "image_url", "image_url": {"url": "https://8.8.8.8/x.png"}},
        ]}]
        await validate_chat_image_urls(messages)  # 不抛

    @pytest.mark.asyncio
    async def test_ignores_text_only_and_none(self):
        await validate_chat_image_urls(None)
        await validate_chat_image_urls([{"role": "user", "content": "just text"}])


class TestVerifyTokenRobustness:
    def test_verify_token_no_secret_returns_false_not_raise(self, monkeypatch):
        """token-only 部署(ADMIN_SESSION_SECRET 空):含 '.' 的伪造 cookie → False,不抛 500。"""
        import src.api.admin_session as sess
        def _boom():
            raise RuntimeError("ADMIN_SESSION_SECRET must be set")
        monkeypatch.setattr(sess, "_secret", _boom)
        # future expiry 以越过前面的过期检查,逼到 _secret() 分支
        forged = f"{int(__import__('time').time()) + 9999}.deadbeef"
        assert sess.verify_token(forged) is False


class TestChatMediaUrls:
    """2026-09-27:vLLM 会 fetch 的不止 type=image_url —— video_url / audio_url / 无 type 块 /
    带 uuid 的块 / input_image 都会被抓取。校验按字段取 URL,不按 type。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("part", [
        {"type": "video_url", "video_url": {"url": "http://169.254.169.254/v.mp4"}},
        {"type": "audio_url", "audio_url": {"url": "http://127.0.0.1:8000/a.wav"}},
        {"image_url": "http://10.0.0.1/x.png"},                       # 无 type,字符串
        {"video_url": {"url": "http://192.168.1.2/v.mp4"}},           # 无 type,dict
        {"type": "image_url", "uuid": "u1", "image_url": {"url": "http://127.0.0.1/x"}},
        {"type": "input_image", "image_url": "http://169.254.169.254/x"},
        {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}},
        {"type": "image_url", "image_url": {"url": "/etc/passwd"}},    # 无 scheme
        {"type": "video_url", "video_url": {"url": "https://127.0.0.1/v.mp4"}},
    ])
    async def test_blocks_every_url_bearing_part(self, part):
        from src.utils.url_security import validate_chat_media_urls
        with pytest.raises(UnsafeURLError):
            await validate_chat_media_urls([{"role": "user", "content": [part]}])

    @pytest.mark.asyncio
    async def test_allows_inline_data_and_public_https(self):
        from src.utils.url_security import validate_chat_media_urls
        await validate_chat_media_urls([{"role": "user", "content": [
            {"type": "text", "text": "hi"},
            "plain string part",
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA"}},
            {"type": "audio_url", "audio_url": {"url": "data:audio/wav;base64,AAAA"}},
            {"type": "image_url", "image_url": {"url": "https://8.8.8.8/x.png"}},
        ]}])

    @pytest.mark.asyncio
    async def test_scans_every_role(self):
        from src.utils.url_security import validate_chat_media_urls
        with pytest.raises(UnsafeURLError):
            await validate_chat_media_urls([
                {"role": "system", "content": "sys"},
                {"role": "assistant", "content": [
                    {"type": "image_url", "image_url": {"url": "http://127.0.0.1/x"}}]},
            ])

    def test_old_name_is_alias(self):
        from src.utils import url_security as us
        assert us.validate_chat_image_urls is us.validate_chat_media_urls


@pytest.mark.asyncio
async def test_chat_completions_rejects_private_video_url(api_client, bearer_headers, mock_vllm):
    r = await api_client.post("/v1/chat/completions", headers=bearer_headers, json={
        "model": "qwen3.5",
        "messages": [{"role": "user", "content": [
            {"type": "video_url", "video_url": {"url": "http://169.254.169.254/v.mp4"}}]}],
    })
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "unsafe_image_url"
    assert mock_vllm.last_request_body is None          # 没发到上游


@pytest.mark.asyncio
async def test_ollama_chat_rejects_private_image_url(api_client, bearer_headers, mock_vllm):
    r = await api_client.post("/api/chat", headers=bearer_headers, json={
        "model": "qwen3.5", "stream": False,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "http://127.0.0.1:8000/x.png"}}]}],
    })
    assert r.status_code == 400, r.text
    assert mock_vllm.last_request_body is None
