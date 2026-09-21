"""Lane A: load_hardware_config 解析覆盖。"""
from src.config import load_hardware_config


def test_load_default_hardware_yaml():
    """configs/hardware.yaml 解析出 groups 列表。

    实际部署(2026-09-20 起,两张 3090 已拔除):Pro 5000 72GB(llm/tts,gpu=0,
    LLM/ASR/embedding/OCR 全在这张)+ Pro 6000 96GB(image,gpu=1,与 ComfyUI
    sidecar 错峰共用);两卡异构,无 NVLink tp 组。
    """
    cfg = load_hardware_config()
    assert "groups" in cfg
    groups = cfg["groups"]
    assert isinstance(groups, list)
    assert len(groups) >= 1
    llm = next(g for g in groups if g["role"] == "llm")
    assert llm["gpus"] == [0]
    assert llm["nvlink"] is False
    assert llm["vram_gb"] == 72
    # image 独占 Pro 6000(与 ComfyUI 错峰共用),tts 与 llm 共用 Pro 5000
    image = next(g for g in groups if g["role"] == "image")
    assert image["gpus"] == [1]
    tts = next(g for g in groups if g["role"] == "tts")
    assert tts["gpus"] == [0]


def test_load_3gpu_template(tmp_path):
    """hardware.3gpu.yaml 样板解析出 3 个 group。"""
    cfg = load_hardware_config(path="configs/hardware.3gpu.yaml")
    ids = {g["id"] for g in cfg["groups"]}
    assert ids == {"image", "llm-tp", "tts"}
    image = next(g for g in cfg["groups"] if g["id"] == "image")
    assert image["gpus"] == [2]
    assert image["nvlink"] is False
    assert image["vram_gb"] == 96


def test_load_missing_file_returns_empty(tmp_path):
    """文件缺失 → fail-soft 返回 {'groups': []}，不抛异常。"""
    missing = tmp_path / "nope.yaml"
    cfg = load_hardware_config(path=str(missing))
    assert cfg == {"groups": []}


def test_load_corrupt_yaml_returns_empty(tmp_path):
    """yaml 损坏 → fail-soft 返回 {'groups': []}。"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("groups: [ this is not: valid: yaml")
    cfg = load_hardware_config(path=str(bad))
    assert cfg == {"groups": []}


def test_load_missing_groups_key_returns_empty(tmp_path):
    """yaml 合法但无 groups 键 → 返回 {'groups': []}。"""
    nogroups = tmp_path / "nogroups.yaml"
    nogroups.write_text("detection:\n  mode: auto\n")
    cfg = load_hardware_config(path=str(nogroups))
    assert cfg == {"groups": []}
