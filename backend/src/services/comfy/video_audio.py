"""上传前给无音轨视频补一条静音音轨(桥的适配层兜底)。

为什么要补:VHS_LoadVideo 的 audio 输出一旦接进下游(如 VOSR2 视频模板把 `["18",2]`
接到 VHS_VideoCombine 的 `audio`),VHS 会懒提取音频;输入**没有音轨**时 ffmpeg 报
"Output file does not contain any stream",VHS 抛 "VHS failed to extract audio",整条
渲染挂在 LoadVideo 上(2026-09-27 真机 smoke)。给同一段视频加一条静音 AAC 就能跑通。

契约:
- 有音轨 → 原字节原样返回(不碰、不重封装);
- 无音轨 → `-c:v copy` 重封装 + 一条 `anullsrc` 静音轨,**视频不重编码**;
- ffprobe/ffmpeg 不存在、超时、非零退出、任何意外 → `logger.warning` + 原字节返回。
  这是兜底,**绝不能让一个本来能跑的上传因为它失败**。

子进程全部走 asyncio + 超时(不阻塞事件循环),磁盘读写走线程,临时目录用完即删。
输入只允许 `file` 协议(`-protocol_whitelist`):调用方给的字节可能是伪装成 mp4 的
HLS 播放列表,不设白名单 ffprobe 会替调用方去抓里面的远程 URL(SSRF)。
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 30
_REMUX_TIMEOUT_S = 180
_SILENCE_SRC = "anullsrc=r=44100:cl=stereo"
# 容器 → 静音轨编码器。webm 只收 vorbis/opus,塞 AAC 会封装失败(失败也只是退回原字节)。
_AUDIO_CODEC_BY_EXT = {"webm": "libopus"}
_DEFAULT_AUDIO_CODEC = "aac"


class _ToolError(RuntimeError):
    """ffprobe/ffmpeg 跑不起来、超时或非零退出。"""


async def _run(argv: list[str], timeout_s: float) -> bytes:
    """跑一个子进程,返回 stdout;任何失败都抛 `_ToolError`(超时会 kill 掉子进程)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:  # 不在 PATH 上 / 没权限
        raise _ToolError(f"无法启动 {argv[0]}: {e}") from e
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise _ToolError(f"{argv[0]} 超时(>{timeout_s}s)") from e
    if proc.returncode != 0:
        tail = stderr.decode(errors="replace").strip()[-300:]
        raise _ToolError(f"{argv[0]} 退出码 {proc.returncode}: {tail}")
    return stdout


async def _has_audio_stream(path: Path) -> bool:
    out = await _run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file",
         "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0",
         str(path)],
        _PROBE_TIMEOUT_S,
    )
    return bool(out.strip())


async def _remux_with_silence(src: Path, dst: Path, ext: str) -> bytes | None:
    """`-c:v copy` + 静音轨重封装;失败返回 None(调用方退回原字节)。"""
    codec = _AUDIO_CODEC_BY_EXT.get(ext, _DEFAULT_AUDIO_CODEC)
    try:
        await _run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error",
             "-protocol_whitelist", "file", "-i", str(src),
             "-f", "lavfi", "-i", _SILENCE_SRC,
             "-map", "0:v", "-map", "1:a", "-shortest",
             "-c:v", "copy", "-c:a", codec, str(dst)],
            _REMUX_TIMEOUT_S,
        )
        return await asyncio.to_thread(dst.read_bytes)
    except (_ToolError, OSError) as e:
        logger.warning("video_audio: 补静音轨失败,上传原视频:%s", e)
        return None


async def ensure_audio_track(raw: bytes, ext: str) -> bytes:
    """无音轨 → 返回补了静音轨的新字节;有音轨或任何失败 → 原样返回 `raw`。"""
    try:
        with tempfile.TemporaryDirectory(prefix="nous-bridge-audio-") as tmp:
            src = Path(tmp) / f"src.{ext}"
            await asyncio.to_thread(src.write_bytes, raw)
            if await _has_audio_stream(src):
                return raw
            remuxed = await _remux_with_silence(src, Path(tmp) / f"out.{ext}", ext)
    except Exception as e:  # noqa: BLE001 —— 兜底层:绝不让上传因它失败
        logger.warning("video_audio: 探测音轨失败,上传原视频:%s", e)
        return raw
    if remuxed is None:
        return raw
    logger.info("video_audio: 输入视频无音轨,已补静音 %s 轨(%d → %d 字节)",
                _AUDIO_CODEC_BY_EXT.get(ext, _DEFAULT_AUDIO_CODEC), len(raw), len(remuxed))
    return remuxed
