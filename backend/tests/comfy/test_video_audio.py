"""`comfy/video_audio.ensure_audio_track`:无音轨视频补一条静音 AAC,有音轨原样不动。

背景:VOSR2 视频模板把 VHS_LoadVideo 的 audio 输出接进了 VideoCombine,VHS 会懒提取
音频 —— 输入没有音轨时 ffmpeg 报 "Output file does not contain any stream",整条渲染
挂在 LoadVideo 上。桥在上传前补一条静音轨绕开。

真 ffmpeg/ffprobe 造几 KB 的 fixture(lavfi `color` + `anullsrc`),机器上没有就 skip。
只起 ffmpeg/ffprobe,不碰 GPU、不起任何推理服务。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import src.services.comfy.video_audio as va
from src.services.comfy.video_audio import ensure_audio_track

_HAVE_FF = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ff = pytest.mark.skipif(not _HAVE_FF, reason="需要 ffmpeg + ffprobe")


def _make_mp4(tmp_path: Path, *, with_audio: bool) -> bytes:
    out = tmp_path / ("a.mp4" if with_audio else "s.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=12:d=0.5"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest",
                "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(cmd, check=True, timeout=60)
    return out.read_bytes()


def _streams(tmp_path: Path, raw: bytes) -> list[dict]:
    p = tmp_path / "probe.mp4"
    p.write_bytes(raw)
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "json", str(p)],
        check=True, capture_output=True, timeout=30)
    return json.loads(r.stdout)["streams"]


@needs_ff
async def test_silent_mp4_gets_silent_aac_track(tmp_path):
    silent = _make_mp4(tmp_path, with_audio=False)
    assert [s["codec_type"] for s in _streams(tmp_path, silent)] == ["video"]

    out = await ensure_audio_track(silent, "mp4")

    streams = _streams(tmp_path, out)
    assert sorted(s["codec_type"] for s in streams) == ["audio", "video"]
    audio = next(s for s in streams if s["codec_type"] == "audio")
    video = next(s for s in streams if s["codec_type"] == "video")
    assert audio["codec_name"] == "aac"
    assert video["codec_name"] == "h264"  # -c:v copy,没重编码


@needs_ff
async def test_audio_bearing_mp4_is_byte_identical(tmp_path):
    with_audio = _make_mp4(tmp_path, with_audio=True)
    out = await ensure_audio_track(with_audio, "mp4")
    assert out is with_audio or out == with_audio


async def test_ffprobe_failure_falls_back_to_original(monkeypatch):
    async def _boom(_path):
        raise OSError("ffprobe 不存在")
    monkeypatch.setattr(va, "_has_audio_stream", _boom)
    raw = b"\x00\x00\x00\x18ftypmp42"
    assert await ensure_audio_track(raw, "mp4") == raw


async def test_missing_binaries_fall_back_to_original(monkeypatch):
    """PATH 上没有 ffprobe/ffmpeg → spawn 抛 OSError → 原样返回,不抛。"""
    monkeypatch.setenv("PATH", "/nonexistent")
    raw = b"\x00\x00\x00\x18ftypmp42"
    assert await ensure_audio_track(raw, "mp4") == raw


@needs_ff
async def test_garbage_bytes_fall_back_to_original():
    """ffprobe 认不出的字节(非零退出)→ 原样返回。"""
    raw = b"definitely not a video"
    assert await ensure_audio_track(raw, "mp4") == raw


@needs_ff
async def test_remux_failure_falls_back_to_original(monkeypatch, tmp_path):
    silent = _make_mp4(tmp_path, with_audio=False)

    async def _fail(*_a, **_kw):
        return None
    monkeypatch.setattr(va, "_remux_with_silence", _fail)
    assert await ensure_audio_track(silent, "mp4") == silent
