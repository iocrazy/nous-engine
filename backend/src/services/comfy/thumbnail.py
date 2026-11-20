"""First-frame thumbnail extraction for video outputs (gallery preview).

Shells out to `ffmpeg` rather than pulling in a Python video decoding dep —
ffmpeg is the de-facto standard already assumed available on boxes that run
ComfyUI-adjacent media pipelines, but it's optional here: a box without it
just gets no thumbnail (caller falls back to a generic video icon), never a
hard failure of the whole workflow run.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


_TIMEOUT_SECONDS = 60


async def extract_first_frame(video_path: Path, *, timeout: float | None = None) -> bytes | None:
    """Return the first frame of `video_path` as PNG bytes, or None.

    Never raises: missing/unreadable ffmpeg binary (OSError — FileNotFoundError,
    PermissionError, ...), a hung/slow-to-decode input (timeout), or a non-zero
    ffmpeg exit code (corrupt/unreadable input, unsupported codec, etc.) all
    degrade to None so gallery rendering never breaks on a bad video.
    """
    timeout = _TIMEOUT_SECONDS if timeout is None else timeout
    with tempfile.TemporaryDirectory(prefix="nous-thumb-") as tmp_dir:
        out_path = Path(tmp_dir) / "frame.png"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-protocol_whitelist", "file,pipe", "-i", str(video_path),
                "-frames:v", "1", "-f", "image2", str(out_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            # FileNotFoundError (no ffmpeg on PATH) is the common case, but
            # PermissionError etc. are equally "can't run ffmpeg" — same
            # degrade-to-None contract, not a partial one (round8 lesson,
            # see node_packages.py install_package_git's timeout comment for
            # the sibling subprocess-hygiene fix this mirrors).
            logger.warning("extract_first_frame: failed to spawn ffmpeg")
            return None

        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.warning(
                "extract_first_frame: ffmpeg timed out (>%ds) for %s",
                timeout, video_path,
            )
            return None

        if returncode != 0:
            logger.warning(
                "extract_first_frame: ffmpeg exited %d for %s", returncode, video_path,
            )
            return None

        try:
            return out_path.read_bytes()
        except OSError:
            return None
