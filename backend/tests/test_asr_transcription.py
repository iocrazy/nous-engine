"""ASR 转写端点单测(spec 2026-07-20-moss-asr-sglang-serving)。

后端已切 MOSS-Transcribe-Diarize SGLang 微服务:这里 mock MOSS 的 HTTP 响应(不起真
服务/不碰 GPU),锁 `_asr_moss_transcribe` 的 verbose_json 解析 + 503 语义,以及端点的
segments 门控与秒数计量。真机 e2e 见 spec §4 PR-2 验收。
"""
import asyncio
import io
import wave

import httpx
import pytest

from fastapi import HTTPException

from src.api.routes.openai_compat import (
    _MOSS_DEFAULT_PROMPT,
    _asr_moss_transcribe,
    _asr_moss_transcribe_chunk,
    _asr_verbose_json,
    _detect_language,
    _merge_segments,
    _moss_wav_chunks,
    _punctuation_density,
    _resolve_moss_base_url,
    _restore_punctuation,
    _wav16k_seconds,
)

# Arc 2:base_url 由端点选址(env override > ModelManager)后传入 _asr_moss_transcribe。
# 单测直接给固定 base_url(不经选址),锁解析/请求形/503 语义。
_BASE = "http://127.0.0.1:8003"


# --- MOSS 微服务响应替身 --------------------------------------------------


class _FakeResp:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    """最小 httpx.AsyncClient 替身:记录请求参数、回放预置响应或抛错。"""

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.last_call = None

    async def post(self, url, **kwargs):
        self.last_call = (url, kwargs)
        if self._exc is not None:
            raise self._exc
        return self._resp


# --- _asr_moss_transcribe:verbose_json 解析 -------------------------------


@pytest.mark.asyncio
async def test_moss_transcribe_multi_speaker():
    # ① 多段多说话人:text 剥掉 [Sxx]/时间戳顺序拼接;segments 抠出 speaker + 秒 float。
    body = {
        "task": "transcribe", "duration": 6.0, "language": "zh",
        "text": "[0.26][S01]你好。[3.24][3.5][S02]再见。[6.0]",
        "segments": [
            {"id": 0, "start": 0.26, "end": 3.24, "text": "[S01]你好。"},
            {"id": 1, "start": 3.5, "end": 6.0, "text": "[S02]再见。"},
        ],
    }
    client = _FakeClient(resp=_FakeResp(200, body))
    text, language, segments = await _asr_moss_transcribe_chunk(client, b"wav", None, 6, _BASE)
    assert text == "你好。再见。"
    assert language == "zh"
    assert segments == [
        {"start": 0.26, "end": 3.24, "speaker": "S01", "text": "你好。"},
        {"start": 3.5, "end": 6.0, "speaker": "S02", "text": "再见。"},
    ]


@pytest.mark.asyncio
async def test_moss_transcribe_missing_speaker_prefix():
    # ④ 防御:段缺 [Sxx] 前缀 → speaker=None,文本原样;顶层无 language → None(不假设有)。
    body = {"segments": [{"start": 0.0, "end": 2.0, "text": "没有前缀的文本"}]}
    client = _FakeClient(resp=_FakeResp(200, body))
    text, language, segments = await _asr_moss_transcribe_chunk(client, b"wav", None, 2, _BASE)
    assert language is None
    assert segments == [
        {"start": 0.0, "end": 2.0, "speaker": None, "text": "没有前缀的文本"},
    ]
    assert text == "没有前缀的文本"


@pytest.mark.asyncio
async def test_moss_transcribe_request_shape():
    client = _FakeClient(resp=_FakeResp(200, {"segments": []}))
    await _asr_moss_transcribe_chunk(client, b"wavbytes", None, 6, _BASE)
    url, kwargs = client.last_call
    assert url.endswith("/v1/audio/transcriptions")
    assert kwargs["data"]["model"] == "moss-transcribe-diarize"
    assert kwargs["data"]["response_format"] == "verbose_json"
    assert kwargs["files"]["file"][1] == b"wavbytes"
    # 6s → max(5120, 6*32+512=704) = 5120(下限保短音频)
    assert kwargs["data"]["max_new_tokens"] == "5120"
    for secs, expected in [(460, 460 * 32 + 512), (600, 600 * 32 + 512)]:
        await _asr_moss_transcribe_chunk(client, b"w", None, secs, _BASE)
        assert client.last_call[1]["data"]["max_new_tokens"] == str(expected)


# --- _asr_moss_transcribe:context → 热词 prompt --------------------------


@pytest.mark.asyncio
async def test_moss_transcribe_context_appends_hotword_prompt():
    # context 非空:multipart 含 prompt,以默认转写指令开头、以热词后缀结尾(保输出契约 + 生效热词)。
    client = _FakeClient(resp=_FakeResp(200, {"segments": []}))
    await _asr_moss_transcribe_chunk(client, b"wav", "  瑞幸咖啡  ", 4, _BASE)
    prompt = client.last_call[1]["data"]["prompt"]
    assert prompt.startswith(_MOSS_DEFAULT_PROMPT)
    assert prompt.endswith("热词提示:瑞幸咖啡")  # context 两端空白已 strip


@pytest.mark.asyncio
async def test_moss_transcribe_no_context_sends_default_prompt():
    # context 空/纯空白:**仍显式传** 我们的默认指令(带标点增补条款,服务端默认没有——
    # 省略即丢快语速标点),且无热词后缀。
    for ctx in (None, "", "   "):
        client = _FakeClient(resp=_FakeResp(200, {"segments": []}))
        await _asr_moss_transcribe_chunk(client, b"wav", ctx, 4, _BASE)
        assert client.last_call[1]["data"]["prompt"] == _MOSS_DEFAULT_PROMPT


# --- _asr_moss_transcribe:503 语义(唯一 ASR 主路,不静默降级) ----------


@pytest.mark.asyncio
async def test_moss_transcribe_unreachable_503():
    # ③ 连接失败 → HTTPException 503。
    client = _FakeClient(exc=httpx.ConnectError("connection refused"))
    with pytest.raises(HTTPException) as ei:
        await _asr_moss_transcribe_chunk(client, b"wav", None, 4, _BASE)
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_moss_transcribe_non_200_503():
    # 非 200 → 503(不把上游错误当结果)。
    client = _FakeClient(resp=_FakeResp(500, {}, "internal error"))
    with pytest.raises(HTTPException) as ei:
        await _asr_moss_transcribe_chunk(client, b"wav", None, 4, _BASE)
    assert ei.value.status_code == 503


# --- 端点:segments 门控 + 秒数计量 ---------------------------------------


def _make_wav16k(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # s16le
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


@pytest.mark.parametrize("duration,expected", [(1200, [600, 600]), (1320.25, [600, 600, 120.25])])
def test_moss_chunks_preserve_all_pcm_frames(duration, expected):
    wav = _make_wav16k(duration)
    chunks = list(_moss_wav_chunks(wav))
    assert [seconds for _, seconds, _ in chunks] == expected
    assert [offset for offset, _, _ in chunks] == [600 * i for i in range(len(expected))]
    frames = []
    for _, _, chunk in chunks:
        with wave.open(io.BytesIO(chunk), "rb") as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
            frames.append(audio.readframes(audio.getnframes()))
    with wave.open(io.BytesIO(wav), "rb") as audio:
        assert b"".join(frames) == audio.readframes(audio.getnframes())


class _ChunkClient:
    def __init__(self, fail_at=None, failure=None):
        self.calls = []
        self.fail_at = fail_at
        self.failure = failure

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == self.fail_at:
            if self.failure:
                raise self.failure
            return _FakeResp(500, text="Request requires more tokens than the thinker KV cache can hold")
        return _FakeResp(200, {"language": "en", "segments": [
            {"start": 0.25, "end": 1.0, "text": f"[S01]Part {len(self.calls)}"},
            {"start": 1.0, "end": 1.5, "text": "[S01] continued."},
            {"start": 1.5, "end": 2.0, "text": "No label."},
        ]})


@pytest.mark.parametrize("duration,expected_calls", [(6, 1), (600, 1), (600.25, 2)])
async def test_moss_chunk_boundary_uses_frames_not_rounded_metering(duration, expected_calls):
    wav = _make_wav16k(duration)
    client = _ChunkClient()
    await _asr_moss_transcribe(client, wav, None, round(duration), _BASE)
    assert len(client.calls) == expected_calls
    if expected_calls == 1:
        assert client.calls[0]["files"]["file"][1] is wav


async def test_moss_long_audio_bounded_requests_global_timestamps_and_speakers():
    client = _ChunkClient()
    text, language, segments = await _asr_moss_transcribe(
        client, _make_wav16k(1320.25), "Test Context", 1320, _BASE,
    )
    assert len(client.calls) == 3
    assert [call["data"]["max_new_tokens"] for call in client.calls] == ["19712", "19712", "5120"]
    assert all(call["data"]["prompt"].endswith("热词提示:Test Context") for call in client.calls)
    assert [seg["start"] for seg in segments[::3]] == [0.25, 600.25, 1200.25]
    assert [seg["end"] for seg in segments[::3]] == [1.0, 601.0, 1201.0]
    assert [seg["speaker"] for seg in segments] == ["S01", "S01", None, "S02", "S02", None, "S03", "S03", None]
    assert text == "Part 1continued.No label.Part 2continued.No label.Part 3continued.No label."
    assert language == "en"
    assert len(_asr_verbose_json(text, language, segments, 1320)["segments"]) == 9


@pytest.mark.parametrize("failure", [None, httpx.ConnectError("unreachable"), asyncio.CancelledError()])
async def test_moss_chunk_failure_stops_without_partial_success_or_retry(failure):
    client = _ChunkClient(fail_at=2, failure=failure)
    error = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else HTTPException
    with pytest.raises(error) as exc:
        await _asr_moss_transcribe(client, _make_wav16k(1320), None, 1320, _BASE)
    assert len(client.calls) == 2
    if error is HTTPException:
        assert exc.value.status_code == 503
        assert "chunk 2 failed" in exc.value.detail


@pytest.mark.parametrize("response_format", ["json", "text", "verbose_json"])
@pytest.mark.parametrize("fail_at", [None, 2])
async def test_long_audio_endpoint_uses_one_task_and_original_duration(
    api_client, bearer_headers, mock_vllm, monkeypatch, response_format, fail_at,
):
    from sqlalchemy import select
    from src.api.routes import openai_compat as oc
    from src.models.execution_task import ExecutionTask

    client = _ChunkClient(fail_at=fail_at)
    wav = _make_wav16k(1321)

    async def normalize(raw):
        return wav

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", _BASE)
    monkeypatch.setattr(oc, "_ffmpeg_to_wav16k", normalize)
    monkeypatch.setattr(oc.httpx, "AsyncClient", lambda **kwargs: client)
    response = await api_client.post(
        "/v1/audio/transcriptions", headers=bearer_headers,
        files={"file": ("long.wav", b"test", "audio/wav")},
        data={"model": "qwen3.5", "response_format": response_format, "punctuate": "false"},
    )
    async with api_client.app.state.async_session_factory() as session:
        tasks = (await session.execute(select(ExecutionTask))).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].input_json["audio_seconds"] == 1321
    if fail_at:
        assert response.status_code == 503
        assert len(client.calls) == 2
        assert "Part 1" not in response.text
        assert tasks[0].status == "failed"
        assert tasks[0].result is None
        assert "chunk 2 failed" in tasks[0].error
        return
    assert response.status_code == 200
    assert len(client.calls) == 3
    if response_format == "text":
        assert "Part 3" in response.text
    else:
        result = response.json()
        assert "Part 3" in result["text"]
        if response_format == "json":
            assert result["usage"]["seconds"] == 1321
            assert "segments" not in result
        else:
            assert result["duration"] == 1321
            assert result["segments"][-1]["end"] == 1202
            assert [s["id"] for s in result["segments"]] == list(range(9))
    assert tasks[0].status == "completed"


@pytest.mark.asyncio
async def test_endpoint_segments_gated_by_timestamps(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # ② timestamps=true 带 segments、=false 不带;⑤ usage.seconds = 自算音频秒(4)。
    import src.api.routes.openai_compat as oc

    # Arc 2:端点先经 _resolve_moss_base_url 选址。设 env override → 走 override 分支,
    # 不依赖 ModelManager(仍锁 env>ModelManager 优先级;_asr_moss_transcribe 已整体 mock)。
    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")

    wav = _make_wav16k(4.0)

    async def _fake_ffmpeg(raw):
        return wav

    async def _fake_moss(client, w, ctx, secs, base_url):
        return ("你好世界", "zh", [
            {"start": 0.0, "end": 4.0, "speaker": "S01", "text": "你好世界"},
        ])

    monkeypatch.setattr(oc, "_ffmpeg_to_wav16k", _fake_ffmpeg)
    monkeypatch.setattr(oc, "_asr_moss_transcribe", _fake_moss)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5", "timestamps": "true"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "你好世界"
    assert body["language"] == "zh"
    assert body["usage"] == {"type": "duration", "seconds": 4}
    assert body["segments"][0]["speaker"] == "S01"

    resp2 = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5"},
    )
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["text"] == "你好世界"
    assert "segments" not in body2


@pytest.mark.asyncio
async def test_endpoint_moss_unreachable_returns_503(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # ③ 端点透传:MOSS 不可达 → 503(不静默降级)。
    import src.api.routes.openai_compat as oc

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")

    wav = _make_wav16k(2.0)

    async def _fake_ffmpeg(raw):
        return wav

    async def _fake_moss(client, w, ctx, secs, base_url):
        raise HTTPException(503, detail="MOSS ASR 服务不可达")

    monkeypatch.setattr(oc, "_ffmpeg_to_wav16k", _fake_ffmpeg)
    monkeypatch.setattr(oc, "_asr_moss_transcribe", _fake_moss)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5"},
    )
    assert resp.status_code == 503, resp.text


# --- Arc 3:直连转写进任务中心(spec §8) --------------------------------


@pytest.mark.asyncio
async def test_endpoint_records_completed_asr_task(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # 成功转写 → 落一条 completed 的 asr task(服务名/时长/预览/段数/说话人);
    # _task_to_dict 派生 type=asr。
    import src.api.routes.openai_compat as oc
    from sqlalchemy import select
    from src.models.execution_task import ExecutionTask
    from src.services.execution_task_serialize import _task_to_dict

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(4.0)
    long_text = "你好世界，" * 40  # >120 字符,验证预览截断

    async def _fake_ffmpeg(raw):
        return wav

    async def _fake_moss(client, w, ctx, secs, base_url):
        return (long_text, "zh", [
            {"start": 0.0, "end": 2.0, "speaker": "S01", "text": "你好世界"},
            {"start": 2.0, "end": 4.0, "speaker": "S02", "text": "再见"},
        ])

    monkeypatch.setattr(oc, "_ffmpeg_to_wav16k", _fake_ffmpeg)
    monkeypatch.setattr(oc, "_asr_moss_transcribe", _fake_moss)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("clip.wav", wav, "audio/wav")},
        data={"model": "qwen3.5", "timestamps": "true", "context": "瑞幸咖啡"},
    )
    assert resp.status_code == 200, resp.text

    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        tasks = (await s.execute(select(ExecutionTask))).scalars().all()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.status == "completed"
    assert t.workflow_id is None
    assert t.workflow_name == "qwen3.5"
    assert t.api_key_id is not None
    assert t.result["segments_count"] == 2
    assert t.result["speakers"] == ["S01", "S02"]
    assert t.result["audio_seconds"] == 4
    assert len(t.result["text"]) == 120  # 预览截 120
    # PR-9:input_json 除回显入参外,带 kind/audio_seconds(running 态派生钩子)。
    assert t.input_json == {
        "model": "qwen3.5", "timestamps": True,
        "context": "瑞幸咖啡", "filename": "clip.wav",
        "kind": "asr", "audio_seconds": 4,
    }
    assert _task_to_dict(t)["type"] == "asr"


@pytest.mark.asyncio
async def test_endpoint_records_failed_asr_task(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # MOSS 不可达 → 503,且落一条 failed task(error 简短,无 result)。
    import src.api.routes.openai_compat as oc
    from sqlalchemy import select
    from src.models.execution_task import ExecutionTask

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(2.0)

    async def _fake_ffmpeg(raw):
        return wav

    async def _fake_moss(client, w, ctx, secs, base_url):
        raise HTTPException(503, detail="MOSS ASR 服务不可达")

    monkeypatch.setattr(oc, "_ffmpeg_to_wav16k", _fake_ffmpeg)
    monkeypatch.setattr(oc, "_asr_moss_transcribe", _fake_moss)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("clip.wav", wav, "audio/wav")},
        data={"model": "qwen3.5"},
    )
    assert resp.status_code == 503, resp.text

    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        tasks = (await s.execute(select(ExecutionTask))).scalars().all()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.status == "failed"
    assert t.workflow_name == "qwen3.5"
    assert t.error == "MOSS ASR 服务不可达"
    assert t.result is None


# --- 计量口径不变(既有断言保住) ----------------------------------------


def test_wav16k_seconds():
    assert _wav16k_seconds(_make_wav16k(4.0)) == 4
    assert _wav16k_seconds(_make_wav16k(0.3)) == 1  # 至少 1 秒计费
    # 损坏/非 wav 字节 → 退化估算不崩(至少 1)
    assert _wav16k_seconds(b"not a wav") >= 1


# --- PR-7:文本语种检测(_detect_language,纯字符集启发式,无第三方依赖) ------


def test_detect_language_covers_scripts():
    assert _detect_language("今天天气很好啊真的不错") == "zh"       # CJK → zh
    assert _detect_language("the quick brown fox") == "en"        # 拉丁 → en
    assert _detect_language("こんにちは、今日はいい天気です") == "ja"  # kana 优先于混排 CJK → ja
    assert _detect_language("안녕하세요 반갑습니다") == "ko"         # 谚文 → ko
    assert _detect_language("Привет как дела") == "ru"            # 西里尔 → ru


def test_detect_language_mixed_takes_majority():
    # 混合(拉丁 4 vs CJK 8)取占比最高 → zh。
    assert _detect_language("这是一个 test 测试用例呀") == "zh"


def test_detect_language_undetectable_is_none():
    assert _detect_language("") is None            # 空文本
    assert _detect_language("123 ... !!! @#$") is None  # 纯数字/标点/空白,无字母


# --- PR-7:verbose_json 形状(_asr_verbose_json,纯函数) ---------------------


def test_asr_verbose_json_shape_and_placeholders():
    segments = [
        {"start": 0.26, "end": 3.24, "speaker": "S01", "text": "你好"},
        {"start": 3.5, "end": 6.0, "speaker": None, "text": "无归属"},
    ]
    out = _asr_verbose_json("你好无归属", "zh", segments, 6)
    assert out["task"] == "transcribe"
    assert out["language"] == "zh"
    assert out["duration"] == 6.0 and isinstance(out["duration"], float)
    assert out["text"] == "你好无归属"
    # 段:id 递增、start/end 保留、speaker 附加字段(null 段为 None)、统计占位中性。
    assert out["segments"][0] == {
        "id": 0, "seek": 0, "start": 0.26, "end": 3.24, "text": "你好",
        "tokens": [], "temperature": 0.0, "avg_logprob": 0.0,
        "compression_ratio": 1.0, "no_speech_prob": 0.0, "speaker": "S01",
    }
    assert out["segments"][1]["id"] == 1
    assert out["segments"][1]["speaker"] is None  # 无 speaker 段 → null


def test_asr_verbose_json_none_language_falls_to_und():
    # language 为 None(引擎/检测都判不出)→ "und"(OpenAI 语义)。
    out = _asr_verbose_json("", None, [], 3)
    assert out["language"] == "und"
    assert out["segments"] == []


# --- PR-7:端点 response_format 消费 -----------------------------------------


def _patch_moss(monkeypatch, wav, ret):
    """把端点内 ffmpeg + MOSS 调用整体 mock 成返回固定 (text, language, segments)。"""
    import src.api.routes.openai_compat as oc

    async def _fake_ffmpeg(raw):
        return wav

    async def _fake_moss(client, w, ctx, secs, base_url):
        return ret

    monkeypatch.setattr(oc, "_ffmpeg_to_wav16k", _fake_ffmpeg)
    monkeypatch.setattr(oc, "_asr_moss_transcribe", _fake_moss)


@pytest.mark.asyncio
async def test_endpoint_verbose_json_full_shape(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # verbose_json:OpenAI-Whisper 全形状 + 隐含分段(不传 timestamps 也带 segments)。
    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(6.0)
    _patch_moss(monkeypatch, wav, ("你好再见", "zh", [
        {"start": 0.26, "end": 3.24, "speaker": "S01", "text": "你好"},
        {"start": 3.5, "end": 6.0, "speaker": "S02", "text": "再见"},
    ]))

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5", "response_format": "verbose_json"},  # 不传 timestamps
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"] == "transcribe"
    assert body["language"] == "zh"
    assert body["duration"] == 6.0
    assert body["text"] == "你好再见"
    # 隐含分段:verbose_json 始终带 segments(等效 timestamps=true)。
    assert len(body["segments"]) == 2
    seg0 = body["segments"][0]
    assert seg0["id"] == 0 and seg0["start"] == 0.26 and seg0["end"] == 3.24
    assert seg0["speaker"] == "S01" and seg0["text"] == "你好"
    # 统计占位中性(MOSS 不产)。
    assert seg0["tokens"] == [] and seg0["seek"] == 0
    assert seg0["temperature"] == 0.0 and seg0["avg_logprob"] == 0.0
    assert seg0["compression_ratio"] == 1.0 and seg0["no_speech_prob"] == 0.0


@pytest.mark.asyncio
async def test_endpoint_verbose_json_und_when_undetectable(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # 引擎无 language + 文本无可判定字母 → language "und"。
    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(2.0)
    _patch_moss(monkeypatch, wav, ("123 !!!", None, []))

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5", "response_format": "verbose_json"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["language"] == "und"


@pytest.mark.asyncio
async def test_endpoint_json_default_no_regression_with_detection(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # 默认(不传 response_format):v1 形状不变;引擎无 language 时文本检测补真值(zh)。
    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(4.0)
    _patch_moss(monkeypatch, wav, ("你好世界大家好呀", None, [
        {"start": 0.0, "end": 4.0, "speaker": "S01", "text": "你好世界大家好呀"},
    ]))

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5"},  # 默认 json,不传 timestamps
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # v1 契约:text 首字段、usage duration、无 segments(未请求 timestamps)。
    assert body["text"] == "你好世界大家好呀"
    assert body["language"] == "zh"  # 引擎没回 → 文本检测补
    assert body["usage"] == {"type": "duration", "seconds": 4}
    assert "segments" not in body
    assert "task" not in body and "duration" not in body  # 不混入 verbose_json 字段


@pytest.mark.asyncio
async def test_endpoint_text_format_returns_plain_text(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # response_format=text → 纯文本 body(text/plain),无 JSON 包裹。
    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(3.0)
    _patch_moss(monkeypatch, wav, ("你好世界", "zh", [
        {"start": 0.0, "end": 3.0, "speaker": "S01", "text": "你好世界"},
    ]))

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5", "response_format": "text"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "你好世界"


@pytest.mark.asyncio
async def test_endpoint_unknown_response_format_400(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # 未知 response_format → 400(清晰 message),且不触发 MOSS 调用。
    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(2.0)

    import src.api.routes.openai_compat as oc

    async def _boom(*a, **k):
        raise AssertionError("未知 format 应在校验阶段 400,不该走到 MOSS")

    monkeypatch.setattr(oc, "_asr_moss_transcribe", _boom)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5", "response_format": "srt"},
    )
    assert resp.status_code == 400, resp.text
    assert "response_format" in resp.text


# --- Arc 2:base_url 选址(env override > ModelManager) --------------------


def test_resolve_moss_base_url_env_overrides_modelmanager(monkeypatch):
    # env 显式设 → 用它,**不**碰 ModelManager(应急/测试 override 优先)。
    import src.api.routes.openai_compat as oc

    def _boom(mm, name):  # 若被调到就说明没走 override 分支
        raise AssertionError("ModelManager 不该在 env override 时被调用")

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:9911/")
    monkeypatch.setattr(oc, "get_vllm_base_url", _boom)
    url = _resolve_moss_base_url(object(), "moss_transcribe_diarize")
    assert url == "http://127.0.0.1:9911"  # 末尾 / 已剥


def test_resolve_moss_base_url_falls_back_to_modelmanager(monkeypatch):
    # env 未设 → 经 ModelManager 只读选址(2026-09-05 spec §4:未加载即抛,不拉起)。
    import src.api.routes.openai_compat as oc

    def _mm(mm, name):
        assert name == "moss_transcribe_diarize"
        return "http://127.0.0.1:8123/"

    monkeypatch.delenv("NOUS_MOSS_ASR_URL", raising=False)
    monkeypatch.setattr(oc, "get_vllm_base_url", _mm)
    url = _resolve_moss_base_url(object(), "moss_transcribe_diarize")
    assert url == "http://127.0.0.1:8123"


# --- PR-8:段合并(_merge_segments,纯函数为主) -----------------------------


def test_merge_segments_fragments_into_sentences():
    # 碎段合并成句:6 个 0.5-2s 短段,句号在第 3/6 段尾 → 断成 2 组(句末标点 + 组时长 ≥ 3s)。
    segs = [
        {"start": 0.0, "end": 1.2, "speaker": "S01", "text": "大家好"},
        {"start": 1.3, "end": 2.5, "speaker": "S01", "text": "今天讲个事"},
        {"start": 2.6, "end": 3.8, "speaker": "S01", "text": "很重要。"},
        {"start": 3.9, "end": 5.0, "speaker": "S01", "text": "第二部分"},
        {"start": 5.1, "end": 6.2, "speaker": "S01", "text": "继续说"},
        {"start": 6.3, "end": 7.5, "speaker": "S01", "text": "就这样。"},
    ]
    assert _merge_segments(segs) == [
        {"start": 0.0, "end": 3.8, "speaker": "S01", "text": "大家好今天讲个事很重要。"},
        {"start": 3.9, "end": 7.5, "speaker": "S01", "text": "第二部分继续说就这样。"},
    ]


def test_merge_segments_speaker_change_not_merged():
    # speaker 切换 → 不跨并(相邻但异说话人各自成组)。
    segs = [
        {"start": 0.0, "end": 1.0, "speaker": "S01", "text": "你好"},
        {"start": 1.1, "end": 2.0, "speaker": "S02", "text": "我也好"},
    ]
    out = _merge_segments(segs)
    assert len(out) == 2
    assert out[0]["speaker"] == "S01" and out[0]["text"] == "你好"
    assert out[1]["speaker"] == "S02" and out[1]["text"] == "我也好"


def test_merge_segments_gap_over_threshold_not_merged():
    # 间隔 > 0.8s(2.0-1.0=1.0)→ 不合并(节奏断点保停顿)。
    segs = [
        {"start": 0.0, "end": 1.0, "speaker": "S01", "text": "你好"},
        {"start": 2.0, "end": 3.0, "speaker": "S01", "text": "再见"},
    ]
    out = _merge_segments(segs)
    assert len(out) == 2
    assert out[0]["text"] == "你好" and out[1]["text"] == "再见"


def test_merge_segments_gap_within_threshold_merged():
    # 间隔 = 0.8s(恰在阈上,<=)→ 合并(边界值)。
    segs = [
        {"start": 0.0, "end": 1.0, "speaker": "S01", "text": "你好"},
        {"start": 1.8, "end": 2.5, "speaker": "S01", "text": "世界"},
    ]
    out = _merge_segments(segs)
    assert len(out) == 1
    assert out[0] == {"start": 0.0, "end": 2.5, "speaker": "S01", "text": "你好世界"}


def test_merge_segments_hard_cap_15s_duration():
    # 组时长 > 15s 硬上限:4 个 6s 段(无句末标点)→ 第 3 段把组推过 18s 封组,断成 2 组。
    segs = [
        {"start": 0.0, "end": 6.0, "speaker": "S01", "text": "一段"},
        {"start": 6.1, "end": 12.0, "speaker": "S01", "text": "二段"},
        {"start": 12.1, "end": 18.0, "speaker": "S01", "text": "三段"},
        {"start": 18.1, "end": 24.0, "speaker": "S01", "text": "四段"},
    ]
    out = _merge_segments(segs)
    assert len(out) == 2
    assert out[0]["start"] == 0.0 and out[0]["end"] == 18.0
    assert out[0]["text"] == "一段二段三段"
    assert out[1]["start"] == 18.1 and out[1]["text"] == "四段"


def test_merge_segments_hard_cap_80_chars():
    # 组字数 > 80 硬上限:4 个 30 字短段(短时长、无标点)→ 第 3 段把组推到 90 字封组,断成 2 组。
    segs = [
        {"start": 0.0, "end": 0.5, "speaker": "S01", "text": "一" * 30},
        {"start": 0.6, "end": 1.1, "speaker": "S01", "text": "二" * 30},
        {"start": 1.2, "end": 1.7, "speaker": "S01", "text": "三" * 30},
        {"start": 1.8, "end": 2.3, "speaker": "S01", "text": "四" * 30},
    ]
    out = _merge_segments(segs)
    assert len(out) == 2
    assert len(out[0]["text"]) == 90  # 30*3,超 80 后封组
    assert len(out[1]["text"]) == 30


def test_merge_segments_english_spacing():
    # 英文/数字边界补空格(防粘连);中文直接串接不加空格。
    en = _merge_segments([
        {"start": 0.0, "end": 1.0, "speaker": None, "text": "good"},
        {"start": 1.1, "end": 2.0, "speaker": None, "text": "morning"},
    ])
    assert len(en) == 1 and en[0]["text"] == "good morning"  # 补空格
    zh = _merge_segments([
        {"start": 0.0, "end": 1.0, "speaker": None, "text": "你好"},
        {"start": 1.1, "end": 2.0, "speaker": None, "text": "世界"},
    ])
    assert len(zh) == 1 and zh[0]["text"] == "你好世界"  # 无空格


def test_merge_segments_empty_and_single_passthrough():
    # 空列表 / 单段原样返回。
    assert _merge_segments([]) == []
    single = [{"start": 0.0, "end": 1.0, "speaker": "S01", "text": "你好"}]
    assert _merge_segments(single) == single


def test_merge_segments_does_not_mutate_input():
    # 不 mutate 入参:输出是新 dict,入参列表与其元素保持原样。
    segs = [
        {"start": 0.0, "end": 1.2, "speaker": "S01", "text": "大家好"},
        {"start": 1.3, "end": 2.5, "speaker": "S01", "text": "世界"},
    ]
    import copy
    snapshot = copy.deepcopy(segs)
    out = _merge_segments(segs)
    assert segs == snapshot  # 入参未被改
    assert out[0] is not segs[0]  # 输出是新对象


# --- PR-8:端点 merge_segments 消费 ------------------------------------------


_MERGE_MOSS_SEGS = [
    {"start": 0.0, "end": 1.2, "speaker": "S01", "text": "大家好"},
    {"start": 1.3, "end": 2.5, "speaker": "S01", "text": "今天讲个事"},
    {"start": 2.6, "end": 3.8, "speaker": "S01", "text": "很重要。"},
    {"start": 3.9, "end": 5.0, "speaker": "S01", "text": "第二部分"},
    {"start": 5.1, "end": 6.2, "speaker": "S01", "text": "继续说"},
    {"start": 6.3, "end": 7.5, "speaker": "S01", "text": "就这样。"},
]


@pytest.mark.asyncio
async def test_endpoint_merge_segments_default_false_no_merge(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # 端点级:不传 merge_segments(默认 False)→ segments 原样(6 段,不合并)。
    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(8.0)
    _patch_moss(monkeypatch, wav, ("大家好今天讲个事很重要。第二部分继续说就这样。",
                                   "zh", [dict(s) for s in _MERGE_MOSS_SEGS]))

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5", "timestamps": "true"},  # 不传 merge_segments
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["segments"]) == 6  # 原样,未合并
    assert body["text"] == "大家好今天讲个事很重要。第二部分继续说就这样。"


@pytest.mark.asyncio
async def test_endpoint_merge_segments_verbose_json_renumbers_ids(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # verbose_json + merge_segments=true:6 碎段 → 2 句级段,id 重新 0..n 编号;
    # 任务中心 segments_count 用合并后数量(用户视角一致);text 全文不变。
    from sqlalchemy import select
    from src.models.execution_task import ExecutionTask

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(8.0)
    full_text = "大家好今天讲个事很重要。第二部分继续说就这样。"
    _patch_moss(monkeypatch, wav, (full_text, "zh", [dict(s) for s in _MERGE_MOSS_SEGS]))

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5", "response_format": "verbose_json",
              "merge_segments": "true"},  # verbose_json 隐含分段,无需 timestamps
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == full_text  # 全文不变
    assert len(body["segments"]) == 2  # 6 → 2
    assert [s["id"] for s in body["segments"]] == [0, 1]  # id 重新 0..n
    assert body["segments"][0]["text"] == "大家好今天讲个事很重要。"
    assert body["segments"][0]["start"] == 0.0 and body["segments"][0]["end"] == 3.8
    assert body["segments"][1]["text"] == "第二部分继续说就这样。"

    # 任务中心记录合并后段数。
    sf = api_client.app.state.async_session_factory
    async with sf() as s:
        tasks = (await s.execute(select(ExecutionTask))).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].result["segments_count"] == 2


# --- PR-10:标点密度(_punctuation_density,纯函数) --------------------------


def test_punctuation_density():
    assert _punctuation_density("") == 0.0            # 空文本
    assert _punctuation_density("你好世界大家好") == 0.0  # 无标点 → 0
    # 6 字 2 标点(中文逗/句号)→ 1/3
    assert _punctuation_density("你好，世界。") == pytest.approx(2 / 6)
    # 英文标点也计数
    assert _punctuation_density("hi, there.") == pytest.approx(2 / 10)
    # 顿号/分号在标点集内
    assert _punctuation_density("甲、乙；丙") == pytest.approx(2 / 5)


# --- PR-10:标点 LLM 选址(_resolve_punct_base_url,只读、绝不按需拉起) -------


def test_resolve_punct_base_url_uses_readonly_never_autoloads(monkeypatch):
    # **只读**:用 get_vllm_base_url —— 绝不按需拉起 LLM(2026-09-05 起懒加载变体已删)。
    import src.api.routes.openai_compat as oc

    def _fake_get(mm, name):
        assert name == "qwen3_8_27b_abliterated_awq"  # 默认引擎(2026-09-05 3.6 退役)
        return "http://127.0.0.1:8000/"

    monkeypatch.delenv("NOUS_PUNCT_LLM_ENGINE", raising=False)
    monkeypatch.setattr(oc, "get_vllm_base_url", _fake_get)
    assert oc._resolve_punct_base_url(object()) == "http://127.0.0.1:8000"  # 末尾 / 已剥


def test_resolve_punct_base_url_env_overrides_engine(monkeypatch):
    # NOUS_PUNCT_LLM_ENGINE 覆盖默认引擎名。
    import src.api.routes.openai_compat as oc

    def _fake_get(mm, name):
        assert name == "my_other_llm"
        return "http://127.0.0.1:8001"

    monkeypatch.setenv("NOUS_PUNCT_LLM_ENGINE", "my_other_llm")
    monkeypatch.setattr(oc, "get_vllm_base_url", _fake_get)
    assert oc._resolve_punct_base_url(object()) == "http://127.0.0.1:8001"


def test_resolve_punct_base_url_not_loaded_returns_none(monkeypatch):
    # engine 未加载 → VLLMNotLoaded → None(静默降级),**不**触发加载。
    import src.api.routes.openai_compat as oc

    def _raise(mm, name):
        raise oc.VLLMNotLoaded("未加载")

    monkeypatch.setattr(oc, "get_vllm_base_url", _raise)
    assert oc._resolve_punct_base_url(object()) is None


# --- PR-10:标点恢复 + 校验回填(_restore_punctuation) ----------------------


def _chat_resp(content: str, status=200) -> _FakeResp:
    """chat/completions 响应替身:content 塞进 choices[0].message.content。"""
    return _FakeResp(status, {"choices": [{"message": {"content": content}}]})


@pytest.mark.asyncio
async def test_restore_punctuation_normal_backfill():
    # 正常恢复:LLM 只加标点、原样返回;剥标点逐字相等 → 采用恢复文本,speaker/时间戳保留。
    segs = [
        {"start": 0.0, "end": 1.0, "speaker": "S01", "text": "你好世界今天天气不错"},
        {"start": 1.0, "end": 2.0, "speaker": "S02", "text": "我们一起去公园散步吧"},
    ]
    client = _FakeClient(resp=_chat_resp(
        "0|你好世界，今天天气不错。\n1|我们一起去公园散步吧？"
    ))
    out = await _restore_punctuation(client, _BASE, segs)
    assert out is not None
    assert out[0]["text"] == "你好世界，今天天气不错。"
    assert out[1]["text"] == "我们一起去公园散步吧？"
    assert out[0]["speaker"] == "S01" and out[0]["start"] == 0.0
    # 不 mutate 入参
    assert segs[0]["text"] == "你好世界今天天气不错"


@pytest.mark.asyncio
async def test_restore_punctuation_request_shape():
    # 请求形:一次 chat/completions;temperature 0、max_tokens=字数*1.3+256、关思考、编号行。
    client = _FakeClient(resp=_chat_resp("0|你好。"))
    await _restore_punctuation(
        client, _BASE, [{"start": 0.0, "end": 1.0, "speaker": None, "text": "你好"}]
    )
    url, kwargs = client.last_call
    assert url.endswith("/v1/chat/completions")
    body = kwargs["json"]
    assert body["temperature"] == 0
    assert body["max_tokens"] == int(2 * 1.3) + 256  # 2 字
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["content"] == "0|你好"  # 编号行(0 起)


@pytest.mark.asyncio
async def test_restore_punctuation_line_count_mismatch_aborts():
    # 行数不齐(LLM 少返一行)→ 整体放弃(None)。
    segs = [
        {"start": 0.0, "end": 1.0, "speaker": "S01", "text": "甲乙丙"},
        {"start": 1.0, "end": 2.0, "speaker": "S01", "text": "丁戊己"},
    ]
    client = _FakeClient(resp=_chat_resp("0|甲乙丙。"))  # 只回 1 行
    assert await _restore_punctuation(client, _BASE, segs) is None


@pytest.mark.asyncio
async def test_restore_punctuation_number_mismatch_aborts():
    # 编号对不上(第二行 n=2 ≠ 1)→ 整体放弃(None)。
    segs = [
        {"start": 0.0, "end": 1.0, "speaker": "S01", "text": "甲乙"},
        {"start": 1.0, "end": 2.0, "speaker": "S01", "text": "丙丁"},
    ]
    client = _FakeClient(resp=_chat_resp("0|甲乙。\n2|丙丁。"))
    assert await _restore_punctuation(client, _BASE, segs) is None


@pytest.mark.asyncio
async def test_restore_punctuation_changed_char_keeps_original():
    # 某行被 LLM 改字(剥标点后 ≠ 原文)→ 该行保留原文;其余通过行仍回填(逐行判,非整体弃)。
    segs = [
        {"start": 0.0, "end": 1.0, "speaker": "S01", "text": "你好世界"},
        {"start": 1.0, "end": 2.0, "speaker": "S01", "text": "今天天气"},
    ]
    # 第 1 行加了标点(合法);第 2 行多塞了个「好」字(幻觉改字)。
    client = _FakeClient(resp=_chat_resp("0|你好世界。\n1|今天天气好"))
    out = await _restore_punctuation(client, _BASE, segs)
    assert out is not None
    assert out[0]["text"] == "你好世界。"   # 通过 → 采用恢复
    assert out[1]["text"] == "今天天气"     # 改字 → 保留原文


@pytest.mark.asyncio
async def test_restore_punctuation_unreachable_returns_none():
    # LLM 不可达(client 抛)→ None(静默降级)。
    client = _FakeClient(exc=httpx.ConnectError("connection refused"))
    segs = [{"start": 0.0, "end": 1.0, "speaker": "S01", "text": "你好世界"}]
    assert await _restore_punctuation(client, _BASE, segs) is None


@pytest.mark.asyncio
async def test_restore_punctuation_non_200_returns_none():
    # 非 200 → None(不把上游错误当结果)。
    segs = [{"start": 0.0, "end": 1.0, "speaker": "S01", "text": "你好世界"}]
    client = _FakeClient(resp=_chat_resp("", status=500))
    assert await _restore_punctuation(client, _BASE, segs) is None


# --- PR-10:端点 punctuate 触发条件 + 兜底 + 与 merge 的顺序 -------------------


_ZERO_PUNCT_TEXT = "你好世界大家好呀" * 12  # 96 字,零标点(len≥80 且密度<0.005)


def _boom_resolve(mm):
    raise AssertionError("不该触发标点选址")


async def _boom_restore(client, base, segments):
    raise AssertionError("不该触发标点恢复")


@pytest.mark.asyncio
async def test_endpoint_punctuate_restores_when_low_density(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # 触发:len≥80 且零标点 + 默认 punctuate(true)→ 顶层 text 与段 text 均回填标点。
    import src.api.routes.openai_compat as oc

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(4.0)
    _patch_moss(monkeypatch, wav, (_ZERO_PUNCT_TEXT, "zh", [
        {"start": 0.0, "end": 4.0, "speaker": "S01", "text": _ZERO_PUNCT_TEXT},
    ]))
    monkeypatch.setattr(oc, "_resolve_punct_base_url", lambda mm: "http://127.0.0.1:9000")

    restored_text = "你好世界，大家好呀。" * 12

    async def _fake_restore(client, base, segments):
        return [{**segments[0], "text": restored_text}]

    monkeypatch.setattr(oc, "_restore_punctuation", _fake_restore)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5", "timestamps": "true"},  # 不传 punctuate → 默认 true
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == restored_text                 # 顶层 text 由段拼回
    assert body["segments"][0]["text"] == restored_text  # 段 text 已回填


@pytest.mark.asyncio
async def test_endpoint_punctuate_false_disables(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # punctuate=false 显式关 → 不选址、不恢复,原文(零标点)原样返回。
    import src.api.routes.openai_compat as oc

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(4.0)
    _patch_moss(monkeypatch, wav, (_ZERO_PUNCT_TEXT, "zh", [
        {"start": 0.0, "end": 4.0, "speaker": "S01", "text": _ZERO_PUNCT_TEXT},
    ]))
    monkeypatch.setattr(oc, "_resolve_punct_base_url", _boom_resolve)
    monkeypatch.setattr(oc, "_restore_punctuation", _boom_restore)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5", "punctuate": "false"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == _ZERO_PUNCT_TEXT  # 未改


@pytest.mark.asyncio
async def test_endpoint_punctuate_skipped_when_text_too_short(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # 文本 < 80 字 → 不触发(短文本密度不稳)。
    import src.api.routes.openai_compat as oc

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(2.0)
    short = "你好世界大家好"  # 7 字 < 80
    _patch_moss(monkeypatch, wav, (short, "zh", [
        {"start": 0.0, "end": 2.0, "speaker": "S01", "text": short},
    ]))
    monkeypatch.setattr(oc, "_resolve_punct_base_url", _boom_resolve)
    monkeypatch.setattr(oc, "_restore_punctuation", _boom_restore)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == short


@pytest.mark.asyncio
async def test_endpoint_punctuate_skipped_when_density_ok(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # 已有充足标点(密度 ≥ 0.005)→ 不触发。
    import src.api.routes.openai_compat as oc

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(4.0)
    punctuated = "你好，世界。" * 20  # 120 字,密度 1/3
    _patch_moss(monkeypatch, wav, (punctuated, "zh", [
        {"start": 0.0, "end": 4.0, "speaker": "S01", "text": punctuated},
    ]))
    monkeypatch.setattr(oc, "_resolve_punct_base_url", _boom_resolve)
    monkeypatch.setattr(oc, "_restore_punctuation", _boom_restore)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == punctuated


@pytest.mark.asyncio
async def test_endpoint_punctuate_silent_degrade_when_llm_not_loaded(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # 触发条件满足但 LLM 未加载(选址 None)→ 不调恢复,静默用原文。
    import src.api.routes.openai_compat as oc

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(4.0)
    _patch_moss(monkeypatch, wav, (_ZERO_PUNCT_TEXT, "zh", [
        {"start": 0.0, "end": 4.0, "speaker": "S01", "text": _ZERO_PUNCT_TEXT},
    ]))
    monkeypatch.setattr(oc, "_resolve_punct_base_url", lambda mm: None)  # 未加载
    monkeypatch.setattr(oc, "_restore_punctuation", _boom_restore)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == _ZERO_PUNCT_TEXT  # 原文


@pytest.mark.asyncio
async def test_endpoint_punctuate_silent_degrade_when_restore_none(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # 选址成功但恢复校验失败/超时(返回 None)→ 静默用原文。
    import src.api.routes.openai_compat as oc

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(4.0)
    _patch_moss(monkeypatch, wav, (_ZERO_PUNCT_TEXT, "zh", [
        {"start": 0.0, "end": 4.0, "speaker": "S01", "text": _ZERO_PUNCT_TEXT},
    ]))
    monkeypatch.setattr(oc, "_resolve_punct_base_url", lambda mm: "http://127.0.0.1:9000")

    async def _restore_none(client, base, segments):
        return None

    monkeypatch.setattr(oc, "_restore_punctuation", _restore_none)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["text"] == _ZERO_PUNCT_TEXT


@pytest.mark.asyncio
async def test_endpoint_punctuate_before_merge_enables_sentence_split(
    api_client, bearer_headers, mock_vllm, monkeypatch,
):
    # 顺序关键:标点恢复在 merge_segments 之前 —— 恢复出的句末标点参与合并断句。
    # 6 个零标点碎段(总 90 字触发);恢复给第 3/6 段补句号 → merge 断成 2 句级组。
    # 若顺序反了(先 merge 后恢复),merge 见零标点 + 90 字未过 80 上限 → 合成 1 大组。
    import src.api.routes.openai_compat as oc

    monkeypatch.setenv("NOUS_MOSS_ASR_URL", "http://127.0.0.1:8003")
    wav = _make_wav16k(8.0)
    raw_segs = [
        {"start": 0.0, "end": 1.2, "speaker": "S01", "text": "大" * 15},
        {"start": 1.3, "end": 2.5, "speaker": "S01", "text": "家" * 15},
        {"start": 2.6, "end": 3.8, "speaker": "S01", "text": "好" * 15},
        {"start": 3.9, "end": 5.0, "speaker": "S01", "text": "你" * 15},
        {"start": 5.1, "end": 6.2, "speaker": "S01", "text": "我" * 15},
        {"start": 6.3, "end": 7.5, "speaker": "S01", "text": "他" * 15},
    ]
    zero_text = "".join(s["text"] for s in raw_segs)  # 90 字,零标点
    _patch_moss(monkeypatch, wav, (zero_text, "zh", [dict(s) for s in raw_segs]))
    monkeypatch.setattr(oc, "_resolve_punct_base_url", lambda mm: "http://127.0.0.1:9000")

    async def _fake_restore(client, base, segments):
        # 只加标点:第 3、6 段句末补句号(合并封组的句末标点判据)。
        out = [dict(s) for s in segments]
        out[2]["text"] = out[2]["text"] + "。"
        out[5]["text"] = out[5]["text"] + "。"
        return out

    monkeypatch.setattr(oc, "_restore_punctuation", _fake_restore)

    resp = await api_client.post(
        "/v1/audio/transcriptions",
        headers=bearer_headers,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": "qwen3.5", "timestamps": "true", "merge_segments": "true"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 恢复的句号让 merge 断成 2 组(证明恢复在合并之前)。
    assert len(body["segments"]) == 2
    assert body["segments"][0]["text"].endswith("。")
    assert body["segments"][0]["start"] == 0.0 and body["segments"][0]["end"] == 3.8
    assert body["segments"][1]["text"].endswith("。")
    # 顶层 text = 恢复后各段拼接(带 2 个句号),合并不改 text。
    assert body["text"] == zero_text[:45] + "。" + zero_text[45:] + "。"
