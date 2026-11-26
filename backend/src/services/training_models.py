from __future__ import annotations

import asyncio
import fcntl
import hashlib
import logging
import os
import shutil
from pathlib import Path

import httpx
from sqlalchemy import select, update

from src.models.database import get_session_factory
from src.models.training import TrainingDownload

logger = logging.getLogger(__name__)
MODEL_ID = "Qwen/Qwen-Image-2.1"
HF_REVISION = "d26bb61231c349cf6b7896fa83353113880e1ba3"
SOURCES = {
    "modelscope": f"https://modelscope.cn/models/{MODEL_ID}/resolve/master",
    "huggingface": f"https://huggingface.co/{MODEL_ID}/resolve/{HF_REVISION}",
}
WEIGHTS = (
    ("transformer/diffusion_pytorch_model-00001-of-00002.safetensors",
     "transformer/diffusion_pytorch_model-00002-of-00002.safetensors"),
    tuple(f"text_encoder/model-{i:05d}-of-00004.safetensors" for i in range(1, 5)),
    ("vae/diffusion_pytorch_model.safetensors",),
)
FILES = {
    WEIGHTS[0][0]: (9968332504, "9e6bc2d641e67bf277895ea8777141044a38f3edb7101bc469b2961dd7c36b4b"),
    WEIGHTS[0][1]: (4261951904, "3aaf234dcbe128530479735854a346b5e3e66283b7c11db56f836bbd1c13ebaa"),
    WEIGHTS[1][0]: (4998056552, "dde00291b5f7fb92013895310a3da0ddba78674df9f10d505d375243dc01fc6f"),
    WEIGHTS[1][1]: (4915962464, "9047faccc0a6d98496a52d55f27be1c94a9c259d1e283fbea0128d054a948d42"),
    WEIGHTS[1][2]: (4915962496, "8c54187654c0176b73ae73785bf791dc9a14c9df7fb4310083a09d42048cb57e"),
    WEIGHTS[1][3]: (2704357976, "5311532aaaeae3259eb6a7b2c600636be1159adf7ded35f53579f7d0e7d43cdd"),
    WEIGHTS[2][0]: (1350989512, "a07a1b7c4ee2966a1b3bdc37de9b4f983d56937e46619f709a80b6e490675417"),
    "processor/added_tokens.json": (707, "c0284b582e14987fbd3d5a2cb2bd139084371ed9acbae488829a1c900833c680"),
    "processor/chat_template.jinja": (5292, "3636d0f0bd6bef02654cdffdc447b79cb2cef8ab02cc75267345946291a489e4"),
    "processor/merges.txt": (1671853, "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"),
    "processor/preprocessor_config.json": (782, "93585062a80db5e8ca038efc7726a3e6411d9db948472d81d63c6303993be8c5"),
    "processor/special_tokens_map.json": (613, "76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd"),
    "processor/tokenizer.json": (11422654, "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"),
    "processor/tokenizer_config.json": (5445, "81ec7bb9530159b326c0bef1d0b6c33d392090524014ea3f0123a3c1eb9c2af5"),
    "processor/video_preprocessor_config.json": (817, "59c5c9eb52182eb14c06ffb10ca9effd29adce5f238a95de23ca14a38dbd2cb1"),
    "processor/vocab.json": (2776833, "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
}
TOTAL_BYTES = sum(size for size, _ in FILES.values())
LIVE = ("queued", "downloading", "verifying", "cancelling")


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def model_components(root: Path | None) -> tuple[Path | None, list[str]]:
    if root is None:
        return None, ["NOUS_TRAINING_MODEL_ROOT"]
    missing_by_location = []
    for candidate in (root / MODEL_ID, root):
        missing = [
            name for name, (size, _) in FILES.items()
            if not (candidate / name).resolve().is_relative_to(root) or file_size(candidate / name) != size
        ]
        if not missing:
            return candidate, []
        missing_by_location.append(missing)
    return None, min(missing_by_location, key=len)


def model_target(root: Path) -> Path:
    canonical = root / MODEL_ID
    if not canonical.exists() and any((root / name.split("/", 1)[0]).is_dir() for name in FILES):
        return root
    return canonical


def download_progress(root: Path | None) -> tuple[int, int | None]:
    if root is None:
        return 0, None
    target = model_target(root)
    staging = root / ".nous-training-download"
    total = 0
    for name, (size, _) in FILES.items():
        if file_size(target / name) == size:
            total += size
        else:
            total += min(size, file_size(staging / name))
    existing = next(path for path in (root, *root.parents) if path.exists())
    return total, shutil.disk_usage(existing).free


class DownloadCancelled(Exception):
    pass


async def _assert_downloading(job_id: str) -> None:
    async with get_session_factory()() as session:
        job = await session.get(TrainingDownload, job_id)
        if job is None or job.state == "cancelling":
            raise DownloadCancelled


async def _download(client: httpx.AsyncClient, source: str, name: str, path: Path, size: int):
    current = file_size(path)
    if current == size:
        return
    if current > size:
        path.unlink()
        current = 0
    headers = {"Accept-Encoding": "identity"}
    if current:
        headers["Range"] = f"bytes={current}-"
    path.parent.mkdir(parents=True, exist_ok=True)
    async with client.stream("GET", f"{SOURCES[source]}/{name}", headers=headers) as response:
        response.raise_for_status()
        if response.status_code == 206:
            if not response.headers.get("Content-Range", "").startswith(f"bytes {current}-"):
                raise ValueError(f"{name} 的下载范围与暂存文件不匹配")
        elif response.status_code == 200:
            current = 0
        else:
            raise ValueError(f"{name} 的下载响应不完整")
        with path.open("ab" if current else "wb") as output:
            async for chunk in response.aiter_raw(chunk_size=1024 ** 2):
                current += len(chunk)
                if current > size:
                    raise ValueError(f"{name} 大于配方清单中的文件大小")
                await asyncio.to_thread(output.write, chunk)
    if current != size:
        raise ValueError(f"{name} 下载未完成")


def _verified(path: Path, checksum: str) -> bool:
    with path.open("rb") as content:
        return hashlib.file_digest(content, "sha256").hexdigest() == checksum


async def _set_state(job_id: str, state: str, error: str | None = None) -> None:
    async with get_session_factory()() as session:
        job = await session.get(TrainingDownload, job_id, with_for_update=True)
        if job:
            if job.state == "cancelling" and state != "completed":
                state, error = "cancelled", None
            job.state, job.error = state, error
            await session.commit()


async def _perform_download(root: Path, job: TrainingDownload) -> None:
    try:
        target = model_target(root)
        staging = root / ".nous-training-download"
        downloaded, free = download_progress(root)
        if free is None or free < TOTAL_BYTES - downloaded + 1024 ** 3:
            raise ValueError("模型目录可用空间不足")
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=60)) as client:
            for name, (size, _) in FILES.items():
                await _assert_downloading(job.id)
                final = target / name
                partial = staging / name
                if not final.resolve().is_relative_to(root) or not partial.resolve().is_relative_to(root):
                    raise ValueError("组件目录超出配置的模型根目录")
                if file_size(final) != size:
                    if final.exists():
                        raise ValueError(f"{name} 与配方不匹配，请选择空目录或修复原组件")
                    download = asyncio.create_task(_download(client, job.source, name, partial, size))
                    try:
                        while not download.done():
                            await asyncio.wait({download}, timeout=1)
                            await _assert_downloading(job.id)
                        await download
                    finally:
                        if not download.done():
                            download.cancel()
                        await asyncio.gather(download, return_exceptions=True)
        await _set_state(job.id, "verifying")
        for name, (_, checksum) in FILES.items():
            await _assert_downloading(job.id)
            final, partial = target / name, staging / name
            if final.is_file():
                continue
            if not await asyncio.to_thread(_verified, partial, checksum):
                partial.unlink(missing_ok=True)
                raise ValueError(f"{name} 校验失败，请重试")
            final.parent.mkdir(parents=True, exist_ok=True)
            os.link(partial, final)
            partial.unlink()
        await _set_state(job.id, "completed")
    except DownloadCancelled:
        await _set_state(job.id, "cancelled")
    except (OSError, ValueError, httpx.HTTPError) as exc:
        message = f"下载连接失败：{type(exc).__name__}" if isinstance(exc, httpx.HTTPError) else str(exc)
        await _set_state(job.id, "failed", message)


async def download_worker(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".nous-training-download.lock").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                async with get_session_factory()() as session:
                    await session.execute(update(TrainingDownload).where(
                        TrainingDownload.state.in_(("downloading", "verifying")),
                    ).values(state="queued"))
                    await session.execute(update(TrainingDownload).where(
                        TrainingDownload.state == "cancelling",
                    ).values(state="cancelled"))
                    job = await session.scalar(select(TrainingDownload).where(TrainingDownload.state == "queued")
                                               .order_by(TrainingDownload.created_at).limit(1).with_for_update())
                    if job:
                        job.state = "downloading"
                    await session.commit()
                if job:
                    await _perform_download(root, job)
            except BlockingIOError:
                pass
            except Exception:
                logger.exception("Training model downloads failed")
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
            await asyncio.sleep(2)
