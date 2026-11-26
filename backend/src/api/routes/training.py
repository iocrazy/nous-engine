from __future__ import annotations

import base64
import hashlib
import os
import shutil
from io import BytesIO
from tempfile import NamedTemporaryFile
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from anyio import to_thread
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_admin import require_admin
from src.api.routes.understand import call_vllm
from src.models.database import get_async_session
from src.models.training import TrainingDataset, TrainingDownload, TrainingRun
from src.services import training_models as models
from src.services import training_studio as studio
from src.services.comfy.client import ComfyError
from src.services.comfy.lora_options import lora_files

router = APIRouter(prefix="/api/v1/training", dependencies=[Depends(require_admin)])
MAX_IMAGE_BYTES = 24 * 1024 ** 2
FORMATS = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"), "WEBP": ("webp", "image/webp")}


class Named(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")


class Caption(BaseModel):
    caption: str = Field(max_length=8000)


class CaptionRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=1200, pattern=r"\S")
    overwrite: bool = False


class RunInput(Named):
    dataset_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    sample_ids: list[str] = Field(min_length=1, max_length=2000)
    allow_blank: bool = False
    epochs: int = Field(default=3, ge=1, le=100)
    repeat: int = Field(default=1, ge=1, le=100)
    rank: int = Field(default=32, ge=1, le=128)
    learning_rate: float = Field(default=0.0001, gt=0, le=0.01)
    max_pixels: int = Field(default=1048576, ge=65536, le=4194304)
    save_steps: int = Field(default=0, ge=0, le=10000)


class PreviewInput(BaseModel):
    checkpoint: str = Field(pattern=r"^(?:step|epoch)-[0-9]+\.safetensors$")
    prompt: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    size: Literal[512, 768, 1024] = 768
    steps: int = Field(default=25, ge=5, le=50)


class PublishInput(BaseModel):
    filename: str = Field(min_length=1, max_length=100, pattern=r"^[\w-]+$")


async def _dataset(session: AsyncSession, dataset_id: str, *, lock: bool = False) -> TrainingDataset:
    value = await session.get(TrainingDataset, dataset_id, with_for_update=lock)
    if value is None:
        raise HTTPException(404, "数据集不存在")
    return value


def _sample(dataset: TrainingDataset, sample_id: str) -> dict:
    for item in dataset.samples:
        if item["id"] == sample_id:
            return item
    raise HTTPException(404, "素材不存在")


async def _run(session: AsyncSession, run_id: str, *, lock: bool = False) -> TrainingRun:
    value = await session.get(TrainingRun, run_id, with_for_update=lock)
    if value is None:
        raise HTTPException(404, "训练任务不存在")
    return value


def _run_view(run: TrainingRun) -> dict:
    return {
        "id": run.id, "dataset_id": run.dataset_id, "name": run.name, "status": run.status,
        "created_at": run.created_at, "started_at": run.started_at, "finished_at": run.finished_at,
        "error": run.error, "kind": run.spec.get("kind", "training"),
        "total_steps": 2 if run.spec.get("kind") == "preview"
        else len(run.spec["samples"]) * run.spec["repeat"] * run.spec["epochs"],
        "source_run": run.spec.get("source_run"),
        "checkpoint": run.spec.get("checkpoint"), "prompt": run.spec.get("prompt"), "seed": run.spec.get("seed"),
        "published": run.spec.get("published"),
    }


@router.get("/preflight")
async def get_preflight():
    return await studio.preflight(studio.settings(), probe_trainer=True)


class ModelDownloadInput(BaseModel):
    source: Literal["modelscope", "huggingface"]


def _download_view(job: TrainingDownload | None, root: Path | None) -> dict:
    downloaded, available = models.download_progress(root)
    target = models.model_target(root) if root is not None else None
    return {
        "id": job.id if job else None, "state": job.state if job else None,
        "source": job.source if job else None, "error": job.error if job else None,
        "bytes": downloaded, "total_bytes": models.TOTAL_BYTES, "available_bytes": available,
        "target": str(target) if target else None,
    }


@router.get("/model-download")
async def read_model_download(session: AsyncSession = Depends(get_async_session)):
    job = await session.scalar(select(TrainingDownload).order_by(TrainingDownload.created_at.desc()).limit(1))
    return await to_thread.run_sync(_download_view, job, studio.settings().models)


@router.post("/model-download", status_code=202)
async def request_model_download(body: ModelDownloadInput, session: AsyncSession = Depends(get_async_session)):
    root = studio.settings().models
    if root is None:
        raise HTTPException(409, "请先配置训练模型根目录")
    await session.execute(select(func.pg_advisory_xact_lock(76115022)))
    current = await session.scalar(select(TrainingDownload).where(TrainingDownload.state.in_(models.LIVE)).limit(1))
    if current is not None:
        raise HTTPException(409, "已有模型下载任务")
    if models.model_components(root)[0]:
        raise HTTPException(409, "训练组件已就绪")
    view = await to_thread.run_sync(_download_view, None, root)
    if view["available_bytes"] is None or view["available_bytes"] < view["total_bytes"] - view["bytes"] + 1024 ** 3:
        raise HTTPException(409, "模型目录可用空间不足")
    job = TrainingDownload(id=uuid4().hex, source=body.source, state="queued")
    session.add(job)
    await session.commit()
    return {**view, "id": job.id, "source": job.source, "state": job.state}


@router.post("/model-download/{job_id}/cancel")
async def stop_model_download(job_id: str, session: AsyncSession = Depends(get_async_session)):
    job = await session.get(TrainingDownload, job_id, with_for_update=True)
    if job is None:
        raise HTTPException(404, "下载任务不存在")
    if job.state not in models.LIVE:
        raise HTTPException(409, "下载任务已结束")
    job.state = "cancelled" if job.state == "queued" else "cancelling"
    await session.commit()
    return await to_thread.run_sync(_download_view, job, studio.settings().models)


@router.get("/datasets")
async def list_datasets(session: AsyncSession = Depends(get_async_session)):
    rows = await session.scalars(select(TrainingDataset).order_by(TrainingDataset.created_at.desc()))
    return [{"id": row.id, "name": row.name, "count": len(row.samples)} for row in rows]


@router.post("/datasets", status_code=201)
async def create_dataset(body: Named, session: AsyncSession = Depends(get_async_session)):
    data = TrainingDataset(id=uuid4().hex, name=body.name.strip(), samples=[])
    session.add(data)
    await session.commit()
    return {"id": data.id, "name": data.name, "count": 0}


@router.get("/datasets/{dataset_id}")
async def get_dataset(dataset_id: str, session: AsyncSession = Depends(get_async_session)):
    data = await _dataset(session, dataset_id)
    return {"id": data.id, "name": data.name, "samples": data.samples}


def _inspect_image(content: bytes) -> tuple[str, str, int, int]:
    try:
        with Image.open(BytesIO(content), formats=list(FORMATS)) as image:
            suffix, media_type = FORMATS[image.format]
            width, height = image.size
            if width * height > 48_000_000 or min(width, height) < 32 or getattr(image, "is_animated", False):
                raise ValueError
            image.verify()
            return suffix, media_type, width, height
    except (UnidentifiedImageError, Image.DecompressionBombError, ValueError, OSError, SyntaxError) as exc:
        raise HTTPException(422, "仅支持 32 像素以上且不超过 4800 万像素的静态 JPEG、PNG、WebP") from exc


@router.post("/datasets/{dataset_id}/samples", status_code=201)
async def upload_sample(
    dataset_id: str, file: UploadFile = File(),
    caption: str = Form(default="", max_length=8000),
    origin: str = Form(default="upload", pattern=r"^(upload|gallery)$"),
    session: AsyncSession = Depends(get_async_session),
):
    content = await file.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "单张素材最大 24 MiB")
    suffix, media_type, width, height = await to_thread.run_sync(_inspect_image, content)
    data = await _dataset(session, dataset_id, lock=True)
    if len(data.samples) >= 2000:
        raise HTTPException(409, "单个数据集最多存放 2000 张图")
    sample_id = uuid4().hex
    entry = {
        "id": sample_id, "file": f"{sample_id}.{suffix}", "name": Path(file.filename or "image").name[:255],
        "caption": caption.strip(), "origin": origin, "type": media_type, "width": width, "height": height,
    }
    target = studio.settings().data / "datasets" / dataset_id / entry["file"]

    def save_image():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    try:
        await to_thread.run_sync(save_image)
        data.samples = [*data.samples, entry]
        await session.commit()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return entry


@router.get("/datasets/{dataset_id}/samples/{sample_id}")
async def sample_image(dataset_id: str, sample_id: str, session: AsyncSession = Depends(get_async_session)):
    entry = _sample(await _dataset(session, dataset_id), sample_id)
    path = studio.settings().data / "datasets" / dataset_id / entry["file"]
    if not path.is_file():
        raise HTTPException(404, "素材文件不存在")
    return FileResponse(path, media_type=entry["type"], headers={"Cache-Control": "private, max-age=3600"})


@router.patch("/datasets/{dataset_id}/samples/{sample_id}")
async def edit_caption(
    dataset_id: str, sample_id: str, body: Caption, session: AsyncSession = Depends(get_async_session),
):
    data = await _dataset(session, dataset_id, lock=True)
    entry = _sample(data, sample_id)
    edited = {**entry, "caption": body.caption.strip()}
    data.samples = [edited if item["id"] == sample_id else item for item in data.samples]
    await session.commit()
    return edited


@router.get("/caption-model")
async def read_caption_model():
    return {"model": os.getenv("NOUS_TRAINING_CAPTION_MODEL")}


def _caption_data_uri(path: Path) -> str:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1024, 1024))
        output = BytesIO()
        image.save(output, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


@router.post("/datasets/{dataset_id}/samples/{sample_id}/caption")
async def caption_sample(dataset_id: str, sample_id: str, body: CaptionRequest,
                         session: AsyncSession = Depends(get_async_session)):
    model = os.getenv("NOUS_TRAINING_CAPTION_MODEL")
    if not model:
        raise HTTPException(409, "尚未配置训练工坊的视觉标注模型")
    data = await _dataset(session, dataset_id)
    sample = _sample(data, sample_id)
    if sample["caption"] and not body.overwrite:
        return {"state": "skipped"}
    await session.rollback()
    path = studio.settings().data / "datasets" / dataset_id / sample["file"]
    try:
        image = await to_thread.run_sync(_caption_data_uri, path)
        result = await call_vllm(image, body.instruction.strip(), model)
        caption = result["text"].strip()
    except (OSError, ValueError, KeyError, AttributeError, TypeError, httpx.HTTPError) as exc:
        raise HTTPException(502, "视觉标注失败") from exc
    if not caption or len(caption) > 8000:
        raise HTTPException(502, "视觉模型未返回有效描述")
    latest = await _dataset(session, dataset_id, lock=True)
    entry = _sample(latest, sample_id)
    if entry["caption"] != sample["caption"]:
        raise HTTPException(409, "素材描述已被编辑")
    edited = {**entry, "caption": caption, "caption_model": model, "caption_instruction": body.instruction}
    latest.samples = [edited if item["id"] == sample_id else item for item in latest.samples]
    await session.commit()
    return {"state": "saved"}


@router.get("/runs")
async def list_runs(session: AsyncSession = Depends(get_async_session)):
    rows = await session.scalars(select(TrainingRun).order_by(TrainingRun.created_at.desc()).limit(100))
    return [_run_view(row) for row in rows]


async def _enqueue(session: AsyncSession, dataset_id: str, name: str, spec: dict) -> dict:
    config = studio.settings()
    environment = await studio.preflight(config, probe_trainer=True)
    if not environment["can_queue"]:
        raise HTTPException(409, "当前模型、训练环境、磁盘或专用 GPU 未就绪")
    run = TrainingRun(id=uuid4().hex, dataset_id=dataset_id, name=name, status="queued", spec={
        **spec, "recipe_revision": studio.TRAINER_REVISION, "gpu": environment["gpu"]["uuid"],
        "model_path": environment["model_path"],
    })
    session.add(run)
    await session.commit()
    return _run_view(run)


@router.post("/runs", status_code=202)
async def create_run(body: RunInput, session: AsyncSession = Depends(get_async_session)):
    data = await _dataset(session, body.dataset_id)
    by_id = {item["id"]: item for item in data.samples}
    if len(set(body.sample_ids)) != len(body.sample_ids) or any(item not in by_id for item in body.sample_ids):
        raise HTTPException(422, "素材选择有重复或不属于当前数据集")
    samples = [by_id[item] for item in body.sample_ids]
    if not body.allow_blank and any(not item["caption"] for item in samples):
        raise HTTPException(422, "请填写描述或明确允许空描述")
    spec = body.model_dump(exclude={"name", "dataset_id", "sample_ids", "allow_blank"})
    return await _enqueue(session, data.id, body.name.strip(), {**spec, "samples": samples})


@router.get("/runs/{run_id}")
async def get_run(run_id: str, session: AsyncSession = Depends(get_async_session)):
    run = await _run(session, run_id)
    metrics = await to_thread.run_sync(studio.run_metrics, studio.settings().data / "runs" / run_id)
    if run.status == "completed":
        metrics["step"] = _run_view(run)["total_steps"]
    if run.spec.get("kind") == "preview" and run.status == "running":
        metrics["step"] = int((studio.settings().data / "runs" / run_id / "base.png").is_file())
    if run.status in studio.ACTIVE:
        metrics["artifacts"] = []
    return {**_run_view(run), **metrics}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, session: AsyncSession = Depends(get_async_session)):
    run = await _run(session, run_id, lock=True)
    if run.status == "queued":
        run.status, run.finished_at = "cancelled", studio.now()
    elif run.status in ("preparing", "running"):
        run.status = "cancelling"
    elif run.status != "cancelling":
        raise HTTPException(409, "训练已结束")
    await session.commit()
    return _run_view(run)


@router.post("/runs/{run_id}/retry", status_code=202)
async def retry_run(run_id: str, session: AsyncSession = Depends(get_async_session)):
    run = await _run(session, run_id)
    if run.status not in ("failed", "interrupted", "cancelled"):
        raise HTTPException(409, "只能重试未完成的训练")
    return await _enqueue(session, run.dataset_id, run.name, {**run.spec, "source_run": run.id})


@router.get("/runs/{run_id}/artifacts/{name}")
async def download_artifact(run_id: str, name: str, session: AsyncSession = Depends(get_async_session)):
    run = await _run(session, run_id)
    if run.status in ("queued", *studio.ACTIVE):
        raise HTTPException(409, "请等待训练结束")
    if not studio.ARTIFACT.fullmatch(name):
        raise HTTPException(404, "产物不存在")
    path = studio.settings().data / "runs" / run_id / name
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "产物不存在")
    return FileResponse(path, filename=name, media_type="application/octet-stream")


@router.post("/runs/{run_id}/preview", status_code=202)
async def start_preview(run_id: str, body: PreviewInput, session: AsyncSession = Depends(get_async_session)):
    run = await _run(session, run_id)
    if run.status != "completed" or run.spec.get("kind") == "preview":
        raise HTTPException(409, "请选择已完成训练的产物")
    path = studio.settings().data / "runs" / run_id / body.checkpoint
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "Checkpoint 不存在")
    return await _enqueue(session, run.dataset_id, f"{run.name[:105]} · 效果预览", {
        **body.model_dump(), "kind": "preview", "source_run": run_id,
    })


@router.get("/runs/{run_id}/preview/{variant}")
async def get_preview(run_id: str, variant: Literal["base", "lora"],
                      session: AsyncSession = Depends(get_async_session)):
    run = await _run(session, run_id)
    if run.status != "completed" or run.spec.get("kind") != "preview":
        raise HTTPException(404, "对比样图尚未生成")
    path = studio.settings().data / "runs" / run_id / f"{variant}.png"
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "样图不存在")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


def _copy_checkpoint(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise ValueError("目标文件名已被占用")
        with source.open("rb") as original, target.open("rb") as existing:
            if hashlib.file_digest(original, "sha256").digest() == hashlib.file_digest(existing, "sha256").digest():
                return
        raise ValueError("目标文件名已被占用")
    with NamedTemporaryFile(dir=target.parent, prefix=".training-", delete=False) as output:
        try:
            with source.open("rb") as original:
                shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
            os.link(output.name, target)
        finally:
            Path(output.name).unlink(missing_ok=True)


@router.post("/runs/{run_id}/publish")
async def publish_checkpoint(run_id: str, body: PublishInput, session: AsyncSession = Depends(get_async_session)):
    run = await _run(session, run_id, lock=True)
    if run.status != "completed" or run.spec.get("kind") != "preview":
        raise HTTPException(409, "请先完成底模与 LoRA 对比预览")
    folder = os.getenv("NOUS_TRAINING_LORA_ROOT")
    if not folder:
        raise HTTPException(409, "尚未配置 ComfyUI 的 LoRA 根目录")
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(409, "ComfyUI 的 LoRA 根目录不存在")
    relative = f"qwen2.1/{body.filename}.safetensors"
    target = root / relative
    if not target.resolve().is_relative_to(root):
        raise HTTPException(409, "目标路径超出 LoRA 根目录")
    source = studio.settings().data / "runs" / run.spec["source_run"] / run.spec["checkpoint"]
    if not source.is_file() or source.is_symlink():
        raise HTTPException(404, "Checkpoint 不存在")
    try:
        await to_thread.run_sync(_copy_checkpoint, source, target)
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(409, "目标名称已被占用，请换个文件名") from exc
    except OSError as exc:
        raise HTTPException(409, "写入 LoRA 目录失败") from exc
    run.spec = {**run.spec, "published": relative}
    await session.commit()
    try:
        listed = relative in await lora_files()
    except (ComfyError, httpx.HTTPError, ValueError):
        listed = False
    return {"path": relative, "comfy_listed": listed}
