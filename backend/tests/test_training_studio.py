import asyncio
import hashlib
import io
import json
import sys
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.api.routes import training
from src.services import training_models as models
from src.services import training_studio as studio
from src.services.training_preview import validate_lora_targets


def png() -> bytes:
    content = io.BytesIO()
    Image.new("RGB", (64, 64), "#329988").save(content, format="PNG")
    return content.getvalue()


def setup_trainer(tmp_path, monkeypatch):
    trainer = tmp_path / "DiffSynth-Studio"
    models = tmp_path / "models"
    config = studio.TrainingConfig(tmp_path / "storage", trainer, models, "GPU-test")
    monkeypatch.setattr(studio, "settings", lambda: config)
    monkeypatch.setattr(studio, "preflight", AsyncMock(return_value={
        "ready": True, "can_queue": True, "gpu_busy": False, "gpu": {"uuid": config.gpu}, "model_path": str(config.models),
    }))
    python = trainer / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    entry = trainer / studio.TRAINER_SCRIPT
    entry.parent.mkdir(parents=True)
    entry.write_text("""
import json
import pathlib
import sys
args = sys.argv
target = pathlib.Path(args[args.index('--output_path') + 1])
metadata = pathlib.Path(args[args.index('--dataset_metadata_path') + 1])
dataset = pathlib.Path(args[args.index('--dataset_base_path') + 1])
sample = json.loads(metadata.read_text())[0]
assert (dataset / sample['image']).is_file()
print(sample['prompt'])
(target / 'loss.csv').write_text('step,key,value\\n1,loss,0.25\\n')
(target / 'epoch-0.safetensors').write_bytes(b'checkpoint fixture')
""")
    return config


async def create_sample(client) -> tuple[str, str]:
    dataset = await client.post("/api/v1/training/datasets", json={"name": "测试照片"})
    assert dataset.status_code == 201
    dataset_id = dataset.json()["id"]
    image = await client.post(f"/api/v1/training/datasets/{dataset_id}/samples", files={
        "file": ("test.png", png(), "image/png"),
    }, data={"caption": "原始训练描述"})
    assert image.status_code == 201
    return dataset_id, image.json()["id"]


@pytest.mark.asyncio
async def test_training_lifecycle_freezes_caption_and_downloads_checkpoint(db_client, db_session, tmp_path, monkeypatch):
    config = setup_trainer(tmp_path, monkeypatch)
    monkeypatch.setattr(studio, "get_session_factory", lambda: async_sessionmaker(
        db_session.bind, expire_on_commit=False,
    ))
    dataset_id, sample_id = await create_sample(db_client)
    image = await db_client.get(f"/api/v1/training/datasets/{dataset_id}/samples/{sample_id}")
    assert image.headers["content-type"] == "image/png"
    queued = await db_client.post("/api/v1/training/runs", json={
        "name": "训练验收", "dataset_id": dataset_id, "sample_ids": [sample_id], "epochs": 1,
    })
    assert queued.status_code == 202, queued.text
    run_id = queued.json()["id"]
    edited = await db_client.patch(f"/api/v1/training/datasets/{dataset_id}/samples/{sample_id}",
                                   json={"caption": "后来改过的描述"})
    assert edited.json()["caption"] == "后来改过的描述"

    job = await run_worker(db_client, config, run_id)
    assert job["status"] == "completed", job
    assert "原始训练描述" in job["log"]
    assert job["step"] == job["total_steps"] == 1
    assert job["loss"] == [{"step": 1, "loss": 0.25}]
    assert job["artifacts"] == ["epoch-0.safetensors"]
    checkpoint = await db_client.get(f"/api/v1/training/runs/{run_id}/artifacts/epoch-0.safetensors")
    assert checkpoint.content == b"checkpoint fixture"
    metadata = json.loads((config.data / "runs" / run_id / "metadata.json").read_text())
    assert metadata[0]["prompt"] == "原始训练描述"


async def run_worker(client, config, run_id):
    worker = asyncio.create_task(studio.training_worker(config))
    try:
        async with asyncio.timeout(10):
            while True:
                job = (await client.get(f"/api/v1/training/runs/{run_id}")).json()
                if job["status"] in ("completed", "failed"):
                    return job
                await asyncio.sleep(0.2)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_preview_and_publish_use_the_chosen_checkpoint(db_client, db_session, tmp_path, monkeypatch):
    config = setup_trainer(tmp_path, monkeypatch)
    monkeypatch.setattr(studio, "get_session_factory", lambda: async_sessionmaker(
        db_session.bind, expire_on_commit=False,
    ))
    dataset_id, sample_id = await create_sample(db_client)
    response = await db_client.post("/api/v1/training/runs", json={
        "name": "训练验收", "dataset_id": dataset_id, "sample_ids": [sample_id],
    })
    train_id = response.json()["id"]
    assert (await run_worker(db_client, config, train_id))["status"] == "completed"
    fake = tmp_path / "fake_preview.py"
    fake.write_text("""
import json, pathlib, sys
config = json.loads(pathlib.Path(sys.argv[1]).read_text())
assert config["prompt"] == "同样的构图"
assert config["seed"] == 37
assert pathlib.Path(config["checkpoint"]).read_bytes() == b"checkpoint fixture"
output = pathlib.Path(config["output"])
for name in ("base", "lora"):
    (output / f"{name}.png").write_bytes(name.encode())
""")
    monkeypatch.setattr(studio, "PREVIEW_SCRIPT", fake)
    path = f"/api/v1/training/runs/{train_id}"
    response = await db_client.post(f"{path}/preview", json={
        "checkpoint": "epoch-0.safetensors", "prompt": "同样的构图", "seed": 37,
    })
    assert response.status_code == 202
    preview_id = response.json()["id"]
    assert (await run_worker(db_client, config, preview_id))["status"] == "completed"
    path = f"/api/v1/training/runs/{preview_id}"
    assert (await db_client.get(f"{path}/preview/lora")).content == b"lora"

    loras = tmp_path / "loras"
    loras.mkdir()
    monkeypatch.setenv("NOUS_TRAINING_LORA_ROOT", str(loras))
    monkeypatch.setattr(training, "lora_files", AsyncMock(return_value=["qwen2.1/style.safetensors"]))
    payload = {"filename": "style"}
    response = await db_client.post(f"{path}/publish", json=payload)
    assert response.json() == {"path": "qwen2.1/style.safetensors", "comfy_listed": True}
    assert (loras / "qwen2.1/style.safetensors").read_bytes() == b"checkpoint fixture"
    assert (await db_client.post(f"{path}/publish", json=payload)).status_code == 200
    (loras / "qwen2.1/style.safetensors").write_bytes(b"another checkpoint")
    assert (await db_client.post(f"{path}/publish", json=payload)).status_code == 409


@pytest.mark.asyncio
async def test_caption_keeps_existing_labels_unless_explicitly_overwritten(db_client, tmp_path, monkeypatch):
    setup_trainer(tmp_path, monkeypatch)
    dataset_id, sample_id = await create_sample(db_client)
    model = AsyncMock(return_value={"text": "新描述"})
    monkeypatch.setenv("NOUS_TRAINING_CAPTION_MODEL", "vision-test")
    monkeypatch.setattr(training, "call_vllm", model)
    path = f"/api/v1/training/datasets/{dataset_id}/samples/{sample_id}/caption"
    assert (await db_client.post(path, json={"instruction": "描述画面"})).json() == {"state": "skipped"}
    model.assert_not_awaited()
    assert (await db_client.post(path, json={"instruction": "描述画面", "overwrite": True})).json() == {"state": "saved"}
    result = (await db_client.get(f"/api/v1/training/datasets/{dataset_id}")).json()
    assert result["samples"][0]["caption"] == "新描述"


@pytest.mark.asyncio
async def test_model_download_resumes_verifies_and_installs(db_client, db_session, tmp_path, monkeypatch):
    config = setup_trainer(tmp_path, monkeypatch)
    monkeypatch.setattr(models, "get_session_factory", lambda: async_sessionmaker(
        db_session.bind, expire_on_commit=False,
    ))
    content = b"model bytes"
    name = "processor/tokenizer.json"
    monkeypatch.setattr(models, "FILES", {name: (len(content), hashlib.sha256(content).hexdigest())})
    monkeypatch.setattr(models, "TOTAL_BYTES", len(content))
    partial = config.models / ".nous-training-download" / name
    partial.parent.mkdir(parents=True)
    partial.write_bytes(content[:4])

    def provider(request):
        assert request.headers["range"] == "bytes=4-"
        assert request.url.host == "huggingface.co"
        return httpx.Response(206, headers={"Content-Range": f"bytes 4-{len(content) - 1}/{len(content)}"},
                              stream=httpx.ByteStream(content[4:]))

    actual_client = httpx.AsyncClient
    monkeypatch.setattr(models.httpx, "AsyncClient", lambda **kw: actual_client(
        transport=httpx.MockTransport(provider), **kw,
    ))
    queued = await db_client.post("/api/v1/training/model-download", json={"source": "huggingface"})
    assert queued.status_code == 202, queued.text
    worker = asyncio.create_task(models.download_worker(config.models))
    try:
        async with asyncio.timeout(10):
            while True:
                status = (await db_client.get("/api/v1/training/model-download")).json()
                if status["state"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.2)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
    assert status["state"] == "completed", status
    assert (config.models / models.MODEL_ID / name).read_bytes() == content
    assert not partial.exists()


@pytest.mark.asyncio
async def test_training_validation_cancel_and_retry(db_client, tmp_path, monkeypatch):
    setup_trainer(tmp_path, monkeypatch)
    dataset_id, sample_id = await create_sample(db_client)
    base = f"/api/v1/training/datasets/{dataset_id}/samples"
    invalid = await db_client.post(base, files={"file": ("vector.svg", b"<svg/>", "image/svg+xml")})
    assert invalid.status_code == 422
    caption = await db_client.patch(f"{base}/{sample_id}", json={"caption": ""})
    assert caption.status_code == 200
    payload = {"name": "训练", "dataset_id": dataset_id, "sample_ids": [sample_id]}
    assert (await db_client.post("/api/v1/training/runs", json=payload)).status_code == 422
    response = await db_client.post("/api/v1/training/runs", json={**payload, "allow_blank": True})
    run_id = response.json()["id"]
    cancelled = await db_client.post(f"/api/v1/training/runs/{run_id}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    retried = await db_client.post(f"/api/v1/training/runs/{run_id}/retry")
    assert retried.status_code == 202
    assert retried.json()["source_run"] == run_id


def test_recipe_requires_all_components_under_model_root(tmp_path):
    model = tmp_path / studio.MODEL_ID
    for entry, (size, _) in models.FILES.items():
        path = model / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as target:
            target.truncate(size)
    assert studio.model_components(tmp_path) == (model, [])
    shard = models.WEIGHTS[0][0]
    (model / shard).unlink()
    assert studio.model_components(tmp_path) == (None, [shard])


def test_preview_rejects_empty_unknown_or_misshapen_lora_weights():
    layer = "transformer_blocks.0.attn.to_q"
    module = SimpleNamespace(named_modules=lambda: [
        (layer, SimpleNamespace(weight=SimpleNamespace(shape=(8, 4)))),
    ])
    down = layer + ".lora_A.weight"
    up = layer + ".lora_B.weight"
    weights = {down: SimpleNamespace(shape=(2, 4)), up: SimpleNamespace(shape=(8, 2))}
    assert validate_lora_targets(module, weights) == 1
    with pytest.raises(ValueError, match="未包含可试载"):
        validate_lora_targets(module, {})
    with pytest.raises(ValueError, match="与底模不匹配"):
        validate_lora_targets(module, {up: weights[up]})
    with pytest.raises(ValueError, match="与底模不匹配"):
        validate_lora_targets(SimpleNamespace(named_modules=lambda: []), weights)
    with pytest.raises(ValueError, match="尺寸"):
        validate_lora_targets(module, {**weights, up: SimpleNamespace(shape=(4, 2))})


@pytest.mark.asyncio
@pytest.mark.parametrize(("free", "ready"), [(96444, True), (94700, False), (36000, False)])
async def test_preflight_allows_idle_cuda_overhead_only(tmp_path, monkeypatch, free, ready):
    trainer = tmp_path / "trainer"
    python = trainer / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    script = trainer / studio.TRAINER_SCRIPT
    script.parent.mkdir(parents=True)
    script.touch()
    model = tmp_path / "model"
    gpu = "GPU-d24ed424-5712-55e9-9b95-77d997ac80dc"
    monkeypatch.setattr(studio, "model_components", lambda _root: (model, []))

    async def command(*args, **_kwargs):
        if args[0] == "git":
            return studio.TRAINER_REVISION
        assert args[0] == "nvidia-smi"
        return f"{gpu}, RTX PRO 6000, {free}, 97887"

    monkeypatch.setattr(studio, "_read_command", command)
    status = await studio.preflight(studio.TrainingConfig(tmp_path, trainer, model, gpu))
    assert status["can_queue"]
    assert status["ready"] is ready
    assert status["gpu_busy"] is not ready
