"""V1' P1 — scanner now walks media/diffusers/<X> at depth 3 and intentionally
skips the component sub-buckets (diffusion_models/, text_encoders/, vae/).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _stub_settings_to(tmp_path, monkeypatch):
    """Point scan_models()/load_model_configs() at a tmp models tree."""
    from src.services import model_scanner as scanner_mod
    from src.config import get_settings as _gs

    settings = MagicMock()
    settings.LOCAL_MODELS_PATH = str(tmp_path)
    monkeypatch.setattr(scanner_mod, "get_settings", lambda: settings)
    _gs.cache_clear()
    # Empty yaml configs so we only see auto-detection in these tests.
    monkeypatch.setattr(scanner_mod, "load_model_configs", lambda **kw: {})


def _make_diffusers_dir(base, rel: str, class_name: str = "Flux2Pipeline"):
    d = base / rel
    d.mkdir(parents=True)
    (d / "model_index.json").write_text(json.dumps({"_class_name": class_name}))
    return d


def test_scanner_finds_diffusers_at_image_diffusers_depth3(tmp_path, monkeypatch):
    """media/diffusers/<X>/ with model_index.json must be auto-detected."""
    _make_diffusers_dir(tmp_path, "media/diffusers/Flux2-klein-9B")
    _make_diffusers_dir(tmp_path, "media/diffusers/ERNIE-Image", "ErnieImagePipeline")
    _stub_settings_to(tmp_path, monkeypatch)

    from src.services.model_scanner import scan_models
    found = scan_models()

    paths = {v["local_path"]: v["type"] for v in found.values()}
    assert paths.get("media/diffusers/Flux2-klein-9B") == "image"
    assert paths.get("media/diffusers/ERNIE-Image") == "image"


def test_scanner_skips_image_component_subdirs(tmp_path, monkeypatch):
    """diffusion_models/, text_encoders/, vae/ hold single-file components,
    not models. They must NOT surface as auto-detected entries even though
    they contain .safetensors files."""
    (tmp_path / "media" / "diffusion_models").mkdir(parents=True)
    (tmp_path / "media" / "diffusion_models" / "Flux2-Klein-9B-True-v2-fp8mixed.safetensors").write_bytes(b"x")
    (tmp_path / "media" / "text_encoders").mkdir(parents=True)
    (tmp_path / "media" / "text_encoders" / "qwen3-8b.safetensors").write_bytes(b"x")
    (tmp_path / "media" / "vae").mkdir(parents=True)
    (tmp_path / "media" / "vae" / "flux2-vae.safetensors").write_bytes(b"x")
    _stub_settings_to(tmp_path, monkeypatch)

    from src.services.model_scanner import scan_models
    found = scan_models()

    for v in found.values():
        lp = v.get("local_path", "")
        assert not lp.startswith("media/diffusion_models/"), lp
        assert not lp.startswith("media/text_encoders/"), lp
        assert not lp.startswith("media/vae/"), lp


def test_scanner_keeps_llm_depth_2(tmp_path, monkeypatch):
    """Non-image trees must stay depth-2 — refuting "scanner walks every type
    deeply" would have broken llm/<Model>/config.json detection."""
    llm_dir = tmp_path / "llm" / "Qwen3-7B"
    llm_dir.mkdir(parents=True)
    (llm_dir / "config.json").write_text(
        json.dumps({"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]})
    )
    _stub_settings_to(tmp_path, monkeypatch)

    from src.services.model_scanner import scan_models
    found = scan_models()
    assert any(v["local_path"] == "llm/Qwen3-7B" for v in found.values())


def test_scanner_finds_embedding_bucket_at_depth_2(tmp_path, monkeypatch):
    """2026-09-11:embedding 是**顶层**桶 `embedding/<MODEL>`,走通用 depth-2 分支。

    原来是 `text/embedding/<MODEL>`,scanner 和 model_metadata_service 各有一段
    depth-3 的 `text` 特例;`text/` 下只有 embedding 一个 bucket,那层是多余的,
    收成顶层桶后两段特例都删了。这条钉住新布局,防止再被当成 depth-3 处理。
    """
    d = tmp_path / "embedding" / "WeMM-Embedding-4B"
    d.mkdir(parents=True)
    (d / "config.json").write_text(
        json.dumps({"model_type": "qwen3_5",
                    "architectures": ["Qwen3_5ForConditionalGeneration"]})
    )
    _stub_settings_to(tmp_path, monkeypatch)

    from src.services.model_scanner import scan_models
    found = scan_models()
    assert any(v["local_path"] == "embedding/WeMM-Embedding-4B" for v in found.values())


def test_scanner_ignores_non_diffusers_image_subdirs(tmp_path, monkeypatch):
    """A novel image/<foo>/ subdir (not in the known set) is ignored rather
    than treated as a model. Adding a new bucket without updating the
    allowlist should be a deliberate change, not a silent surface."""
    weird = tmp_path / "media" / "random_other_bucket"
    weird.mkdir(parents=True)
    (weird / "noise.safetensors").write_bytes(b"x")
    _stub_settings_to(tmp_path, monkeypatch)

    from src.services.model_scanner import scan_models
    found = scan_models()
    assert all(
        not v.get("local_path", "").startswith("image/random_other_bucket")
        for v in found.values()
    )


@pytest.mark.asyncio
async def test_api_v1_models_endpoint_returns_scanner_output(client, monkeypatch):
    """GET /api/v1/models returns scanner output as a sorted list. Schema is
    the same dict scan_models() produces, just keyed-list-shaped for stable
    ETags."""
    from src.api.routes import models as models_route
    from src.api.response_cache import invalidate

    invalidate("models")  # clear any prior test's cache

    fake = {
        "b-model": {"name": "b", "type": "llm", "local_path": "llm/b"},
        "a-model": {"name": "a", "type": "image", "local_path": "media/diffusers/a"},
    }
    monkeypatch.setattr(models_route, "scan_models", lambda: fake)

    resp = await client.get("/api/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["models"], list)
    assert [m["id"] for m in body["models"]] == ["a-model", "b-model"]
    assert body["models"][0]["type"] == "image"
    invalidate("models")


def test_scanner_skips_nous_external_marker(tmp_path, monkeypatch):
    """带 .nous-external 标记的目录 = 外部微服务托管(如 MOSS 走 nous-moss-asr systemd),
    不得生成引擎卡 —— ModelManager 不认其架构,点加载必报 Unknown engine。"""
    import json

    for name, marked in (("MOSS-Transcribe-Diarize", True), ("Some-LLM", False)):
        d = tmp_path / "speech" / name
        d.mkdir(parents=True)
        (d / "config.json").write_text(json.dumps({"model_type": "x", "architectures": ["XForCausalLM"]}))
        if marked:
            (d / ".nous-external").write_text("externally managed\n")
    _stub_settings_to(tmp_path, monkeypatch)

    from src.services.model_scanner import scan_models
    found = scan_models()

    paths = {v["local_path"] for v in found.values()}
    assert "speech/MOSS-Transcribe-Diarize" not in paths
    assert "speech/Some-LLM" in paths
