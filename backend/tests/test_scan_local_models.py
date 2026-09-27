"""scan_local_models 的非图像行为 + models.yaml 无 image 条目守卫。

从 test_image_model_integration.py 挪来(2026-09-26 自建图像引擎删除,Task 4):那个文件
按计划整删,其中 media/ depth-3 与图像 preload 用例随图像引擎删掉;这里保留存活行为的断言
(逐字不变)。
"""
from __future__ import annotations

from unittest.mock import MagicMock


def test_scan_local_models_lists_depth2_llm_dir(tmp_path, monkeypatch):
    """llm/<NAME> 在 depth 2 被列出。"""
    base = tmp_path / "models"
    (base / "llm" / "qwen35-35b-a3b").mkdir(parents=True)

    from src.services import model_metadata_service as svc
    from src.config import get_settings as _gs

    settings = MagicMock()
    settings.LOCAL_MODELS_PATH = str(base)
    monkeypatch.setattr(svc, "get_settings", lambda: settings)
    _gs.cache_clear()

    found = svc.scan_local_models()
    assert "llm/qwen35-35b-a3b" in found


def test_scan_local_models_skips_files(tmp_path, monkeypatch):
    """Files (not dirs) under <type>/ should not contribute entries."""
    base = tmp_path / "models"
    (base / "llm").mkdir(parents=True)
    (base / "llm" / "stray-file.safetensors").write_bytes(b"x")

    from src.services import model_metadata_service as svc
    settings = MagicMock()
    settings.LOCAL_MODELS_PATH = str(base)
    monkeypatch.setattr(svc, "get_settings", lambda: settings)

    found = svc.scan_local_models()
    assert "llm/stray-file.safetensors" not in found
    assert found == set()  # only dirs count


def test_models_yaml_has_no_image_entries():
    """图像模型不在 registry 登记(出图只走 ComfyUI 桥)。"""
    from src.config import load_model_configs
    cfgs = load_model_configs()
    assert not [k for k, v in cfgs.items() if v.get("type") == "image"], \
        "models.yaml 不应再有 image 型条目 —— 图像走组件扫描器"
