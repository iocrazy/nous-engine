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

触发条件是 data URI 声明的 `video/*` mime(桥侧判),file/media 等泛型参数带视频也会处理。

子进程全部走 asyncio + 超时(不阻塞事件循环),并发由 `_FF_SEM` 限到 2;磁盘读写走线程,
临时目录用完即删。

调用方的字节不可信,两道互补的防线,各管一件事:
- **`-f <demuxer>` 钉死输入格式**(按扩展名,mp4/mov → mov,webm/mkv → matroska)——
  防 demuxer 混淆:ffconcat / HLS 这类文本清单伪装成 mp4 时不会被自动探测成清单去读
  别的文件或 URL,只会按 mov/matroska 解析失败 → 原字节上传。这是主防线。
- **`-protocol_whitelist file`** —— 纵深防御:即便某个容器内部引用了外部资源,也只能
  走本地文件协议,打不出网络请求(SSRF)。它**不**防读本地别的文件,那靠上一条。
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
# 扩展名 → 输入 demuxer(与 upload_inputs._VIDEO_MIMES 的四种容器一一对应)。钉死之后
# ffconcat/HLS 等文本清单即使伪装成 mp4 也只会被当 mov/matroska 解析失败;不在表里的
# 扩展名直接跳过,不交给自动探测。
_DEMUXER_BY_EXT = {"mp4": "mov", "mov": "mov", "webm": "matroska", "mkv": "matroska"}
# ffprobe/ffmpeg 并发上限:一波视频上传不该在推理主机上起无上限的子进程。
# 与桥的渲染信号量 `_SEM` 无关(本函数在拿 `_SEM` 之前就跑完)。
_FF_SEM = asyncio.Semaphore(2)


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


def _pinned_input(path: Path, demuxer: str) -> list[str]:
    """首个输入的参数:钉死 demuxer + 只放行 file 协议,绝不交给自动探测。"""
    return ["-protocol_whitelist", "file", "-f", demuxer, "-i", str(path)]


async def _has_audio_stream(path: Path, demuxer: str) -> bool:
    out = await _run(
        ["ffprobe", "-v", "error",
         "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0",
         *_pinned_input(path, demuxer)],
        _PROBE_TIMEOUT_S,
    )
    return bool(out.strip())


async def _remux_with_silence(src: Path, dst: Path, ext: str, demuxer: str) -> bytes | None:
    """`-c:v copy` + 静音轨重封装;失败返回 None(调用方退回原字节)。"""
    codec = _AUDIO_CODEC_BY_EXT.get(ext, _DEFAULT_AUDIO_CODEC)
    try:
        await _run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error",
             *_pinned_input(src, demuxer),
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
    """无音轨 → 返回补了静音轨的新字节;有音轨、容器不认识或任何失败 → 原样返回 `raw`。

    调用方(桥)按 data URI 声明的 `video/*` mime 触发,所以 file/media 这类泛型参数里
    带的视频也会过这一遍,不只 `type: video` 的参数。
    """
    demuxer = _DEMUXER_BY_EXT.get(ext)
    if demuxer is None:
        logger.info("video_audio: 扩展名 %r 没有可钉死的 demuxer,跳过音轨检查", ext)
        return raw
    try:
        async with _FF_SEM:
            with tempfile.TemporaryDirectory(prefix="nous-bridge-audio-") as tmp:
                src = Path(tmp) / f"src.{ext}"
                await asyncio.to_thread(src.write_bytes, raw)
                if await _has_audio_stream(src, demuxer):
                    return raw
                remuxed = await _remux_with_silence(
                    src, Path(tmp) / f"out.{ext}", ext, demuxer)
    except Exception as e:  # noqa: BLE001 —— 兜底层:绝不让上传因它失败
        logger.warning("video_audio: 探测音轨失败,上传原视频:%s", e)
        return raw
    if remuxed is None:
        return raw
    logger.info("video_audio: 输入视频无音轨,已补静音 %s 轨(%d → %d 字节)",
                _AUDIO_CODEC_BY_EXT.get(ext, _DEFAULT_AUDIO_CODEC), len(raw), len(remuxed))
    return remuxed
