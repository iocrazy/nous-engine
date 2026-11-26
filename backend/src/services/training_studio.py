from __future__ import annotations

import asyncio
import csv
import fcntl
import json
import logging
import math
import os
import re
import shutil
import signal
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

from src.models.database import get_session_factory
from src.models.training import TrainingRun
from src.services.training_models import MODEL_ID, WEIGHTS, model_components

logger = logging.getLogger(__name__)
TRAINER_REVISION = "4f67cdf053341ee19a1995b99f3edce76ec11d1c"
TRAINER_SCRIPT = "examples/qwen_image_21/model_training/train.py"
PREVIEW_SCRIPT = Path(__file__).with_name("training_preview.py")
ACTIVE = ("preparing", "running", "cancelling")
ARTIFACT = re.compile(r"(?:step|epoch)-[0-9]+\.safetensors")
GPU_UUID = re.compile(r"GPU-[0-9a-fA-F-]{36}")
MIN_FREE_MIB = 40 * 1024


@dataclass(frozen=True)
class TrainingConfig:
    data: Path
    trainer: Path | None
    models: Path | None
    gpu: str


def settings() -> TrainingConfig:
    def path(key: str) -> Path | None:
        value = os.getenv(key)
        return Path(value).expanduser().resolve() if value else None

    return TrainingConfig(
        path("NOUS_TRAINING_DATA_DIR") or (Path.home() / ".local/share/nous-engine/training").resolve(),
        path("NOUS_TRAINING_DIFFSYNTH_DIR"),
        path("NOUS_TRAINING_MODEL_ROOT"),
        os.getenv("NOUS_TRAINING_GPU_UUID", ""),
    )


def now() -> datetime:
    return datetime.now(timezone.utc)


async def _read_command(*args: str, timeout: int = 8, env: dict | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise ValueError("命令返回失败")
    return output.decode(errors="replace").strip()


def trainer_env(gpu: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LD_LIBRARY_PATH") if key in os.environ}
    return {
        **env, "CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": gpu,
        "DIFFSYNTH_SKIP_DOWNLOAD": "True", "HF_HUB_OFFLINE": "1",
        "PYTHONUNBUFFERED": "1",
    }


async def preflight(config: TrainingConfig, *, probe_trainer: bool = False) -> dict:
    model, missing = model_components(config.models)
    python = config.trainer / ".venv/bin/python" if config.trainer else None
    script = config.trainer / TRAINER_SCRIPT if config.trainer else None
    trainer_error = None
    if not python or not script or not os.access(python, os.X_OK) or not script.is_file():
        trainer_error = "请配置 DiffSynth-Studio 目录及其独立 .venv"
    else:
        try:
            revision = await _read_command("git", "-C", str(config.trainer), "rev-parse", "HEAD")
            if revision != TRAINER_REVISION:
                trainer_error = "DiffSynth-Studio 版本与训练配方不匹配"
            elif probe_trainer:
                await _read_command(str(python), str(script), "--help", timeout=25, env=trainer_env(""))
        except (OSError, ValueError, TimeoutError):
            trainer_error = "无法确认 DiffSynth-Studio 版本或训练依赖"

    gpu_error = None
    gpu_info = None
    gpu_busy = False
    if not GPU_UUID.fullmatch(config.gpu):
        gpu_error = "请配置专用训练 GPU 的 UUID"
    else:
        try:
            info = await _read_command(
                "nvidia-smi", "--id", config.gpu, "--query-gpu=uuid,name,memory.free,memory.total",
                "--format=csv,noheader,nounits",
            )
            uuid, name, free, total = next(csv.reader([info], skipinitialspace=True))
            if uuid != config.gpu:
                raise ValueError("GPU UUID mismatch")
            gpu_info = {"uuid": uuid, "name": name, "free_mib": int(free), "total_mib": int(total)}
            gpu_busy = int(total) >= MIN_FREE_MIB and (int(free) < MIN_FREE_MIB or int(total) - int(free) > 1024)
            if int(total) < MIN_FREE_MIB:
                gpu_error = "训练配方要求至少 40 GiB 显存"
            elif gpu_busy:
                gpu_error = "训练 GPU 正在使用，任务会等待设备空闲"
        except (OSError, ValueError, TimeoutError):
            gpu_error = "无法读取训练 GPU 状态"

    storage_error = None
    free_bytes = None
    existing = next((path for path in [config.data, *config.data.parents] if path.exists()), None)
    if existing is not None:
        free_bytes = shutil.disk_usage(existing).free
    if free_bytes is None or free_bytes < 2 * 1024 ** 3 or not os.access(existing, os.W_OK):
        storage_error = "训练目录不可写或可用空间不足"
    can_queue = bool(model) and not any((trainer_error, gpu_error and not gpu_busy, storage_error))
    return {
        "ready": can_queue and not gpu_busy, "can_queue": can_queue, "gpu_busy": gpu_busy,
        "recipe": MODEL_ID, "revision": TRAINER_REVISION, "model_path": str(model) if model else None,
        "missing": missing, "trainer_path": str(config.trainer) if config.trainer else None,
        "trainer_error": trainer_error, "gpu": gpu_info, "gpu_error": gpu_error,
        "storage_error": storage_error, "free_bytes": free_bytes,
    }


def weight_paths(model: Path) -> list:
    paths = [[str(model / item) for item in group] for group in WEIGHTS]
    return [paths[0], paths[1], paths[2][0]]


def preview_command(config: TrainingConfig, model: Path, output: Path, spec: dict) -> list[str]:
    assert config.trainer is not None
    checkpoint = config.data / "runs" / spec["source_run"] / spec["checkpoint"]
    if not checkpoint.is_file() or checkpoint.is_symlink():
        raise ValueError("选定的 checkpoint 不存在")
    config_file = output / "preview.json"
    config_file.write_text(json.dumps({
        **{key: spec[key] for key in ("prompt", "seed", "size", "steps")},
        "checkpoint": str(checkpoint), "output": str(output),
        "processor": str(model / "processor"), "weights": weight_paths(model),
    }, ensure_ascii=False), encoding="utf-8")
    return [str(config.trainer / ".venv/bin/python"), str(PREVIEW_SCRIPT), str(config_file)]


def training_command(config: TrainingConfig, model: Path, dataset: Path, directory: Path, spec: dict) -> list[str]:
    assert config.trainer is not None
    return [
        str(config.trainer / ".venv/bin/python"), str(config.trainer / TRAINER_SCRIPT),
        "--dataset_base_path", str(dataset), "--dataset_metadata_path", str(directory / "metadata.json"),
        "--data_file_keys", "image", "--model_paths", json.dumps(weight_paths(model)),
        "--processor_path", str(model / "processor"), "--output_path", str(directory),
        "--max_pixels", str(spec["max_pixels"]), "--dataset_repeat", str(spec["repeat"]),
        "--num_epochs", str(spec["epochs"]), "--learning_rate", str(spec["learning_rate"]),
        "--lora_rank", str(spec["rank"]), "--remove_prefix_in_ckpt", "pipe.dit.",
        "--lora_base_model", "dit", "--lora_target_modules", "",
        "--use_gradient_checkpointing", "--enable_csv_log",
        *(["--save_steps", str(spec["save_steps"])] if spec["save_steps"] else []),
    ]


def run_metrics(directory: Path) -> dict:
    points = deque(maxlen=200)
    loss_file = directory / "loss.csv"
    if loss_file.is_file():
        with loss_file.open("rb") as stream:
            header = stream.readline().decode(errors="replace")
            start = max(stream.tell(), loss_file.stat().st_size - 32_000)
            if start > stream.tell():
                stream.seek(start)
                stream.readline()
            tail = stream.read(32_000).decode(errors="replace")
            for row in csv.DictReader([header, *tail.splitlines()]):
                try:
                    value = float(row["value"])
                    if row["key"] == "loss" and math.isfinite(value):
                        points.append({"step": int(row["step"]), "loss": value})
                except (KeyError, TypeError, ValueError):
                    continue
    log_file = directory / "train.log"
    log = ""
    if log_file.is_file():
        with log_file.open("rb") as stream:
            stream.seek(max(0, log_file.stat().st_size - 24_000))
            log = stream.read(24_000).decode(errors="replace").replace("\r", "\n")
    artifacts = sorted(path.name for path in directory.glob("*.safetensors")
                       if ARTIFACT.fullmatch(path.name) and path.is_file() and not path.is_symlink())
    return {"step": points[-1]["step"] if points else 0, "loss": list(points), "log": log, "artifacts": artifacts}


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def _update(run_id: str, values: dict, statuses: tuple[str, ...]) -> bool:
    async with get_session_factory()() as session:
        result = await session.execute(update(TrainingRun).where(
            TrainingRun.id == run_id, TrainingRun.status.in_(statuses),
        ).values(**values).returning(TrainingRun.id))
        updated = result.scalar_one_or_none() is not None
        await session.commit()
        return updated


async def _execute(config: TrainingConfig, run: TrainingRun, lease: int) -> None:
    status, error = "failed", None
    process = None
    try:
        check = await preflight(config)
        if not check["can_queue"] or check["gpu"]["uuid"] != run.spec["gpu"] or check["model_path"] != run.spec["model_path"]:
            raise ValueError("训练环境或模型路径已变化。请检查模型、磁盘、环境和独立 GPU 后重试。")
        if check["gpu_busy"]:
            await _update(run.id, {"status": "queued", "started_at": None}, ("preparing",))
            return
        dataset = config.data / "datasets" / run.dataset_id
        output = config.data / "runs" / run.id
        output.mkdir(parents=True, exist_ok=False)
        preview = run.spec.get("kind") == "preview"
        model = Path(check["model_path"])
        if preview:
            command = preview_command(config, model, output, run.spec)
        else:
            metadata = [{"image": sample["file"], "prompt": sample["caption"]} for sample in run.spec["samples"]]
            (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
            command = training_command(config, model, dataset, output, run.spec)
        if not await _update(run.id, {"status": "running"}, ("preparing",)):
            return
        with (output / "train.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=config.trainer, env=trainer_env(run.spec["gpu"]),
                stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=True, pass_fds=(lease,),
            )
        while process.returncode is None:
            await asyncio.sleep(1)
            async with get_session_factory()() as session:
                state = await session.scalar(select(TrainingRun.status).where(TrainingRun.id == run.id))
            if state == "cancelling":
                await _stop(process)
                return
        if process.returncode != 0:
            raise ValueError(f"训练器退出，状态码 {process.returncode}。请查看日志。")
        if preview:
            if not all((output / name).is_file() for name in ("base.png", "lora.png")):
                raise ValueError("训练器未生成完整的对比样图")
        elif not run_metrics(output)["artifacts"]:
            raise ValueError("训练器未产出 checkpoint。请查看日志。")
        status = "completed"
    except asyncio.CancelledError:
        if process is not None:
            await _stop(process)
        status, error = "interrupted", "训练进程已停止。可以用同一数据快照重新提交。"
        raise
    except (OSError, ValueError) as exc:
        error = str(exc)
    finally:
        if process is not None and process.returncode is None:
            await _stop(process)
        if not await _update(run.id, {"status": status, "error": error, "finished_at": now()}, ("preparing", "running")):
            await _update(run.id, {"status": "cancelled", "finished_at": now()}, ("cancelling",))


async def training_worker(config: TrainingConfig) -> None:
    config.data.mkdir(parents=True, exist_ok=True)
    with (config.data / ".worker").open("a") as lock:
        acquired = False
        while True:
            try:
                if not acquired:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                async with get_session_factory()() as session:
                    await session.execute(update(TrainingRun).where(TrainingRun.status.in_(ACTIVE)).values(
                        status="interrupted", error="训练进程已停止。可以用同一数据快照重新提交。", finished_at=now(),
                    ))
                    run = await session.scalar(select(TrainingRun).where(TrainingRun.status == "queued")
                                               .order_by(TrainingRun.created_at).limit(1).with_for_update())
                    if run is not None:
                        run.status, run.started_at = "preparing", now()
                    await session.commit()
                if run is not None:
                    await _execute(config, run, lock.fileno())
            except BlockingIOError:
                pass
            except Exception:
                logger.exception("Training worker failed")
            await asyncio.sleep(2)
