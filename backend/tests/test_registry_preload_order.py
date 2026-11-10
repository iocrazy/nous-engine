"""Lane H: ModelSpec.preload_order 字段解析测试。"""
import textwrap
from pathlib import Path

from src.services.inference.registry import ModelRegistry, ModelSpec


def _write_yaml(tmp_path: Path, body: str) -> str:
    p = tmp_path / "models.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_preload_order_defaults_to_none():
    """yaml 条目没写 preload_order → spec.preload_order 是 None。"""
    spec = ModelSpec(
        id="m", model_type="image", adapter_class="fake",
        paths={"main": "/fake"}, vram_mb=1024,
    )
    assert spec.preload_order is None


def test_preload_order_read_from_yaml(tmp_path):
    """yaml 条目写了 preload_order → registry 读进 spec。"""
    yaml_path = _write_yaml(tmp_path, """
        models:
          - id: early
            type: image
            adapter: fake.Adapter
            paths: {main: /fake/early}
            vram_mb: 1024
            resident: true
            preload_order: 10
          - id: late
            type: tts
            adapter: fake.Adapter
            paths: {main: /fake/late}
            vram_mb: 512
            resident: true
            preload_order: 20
          - id: unordered
            type: image
            adapter: fake.Adapter
            paths: {main: /fake/unordered}
            vram_mb: 256
            resident: true
    """)
    reg = ModelRegistry(yaml_path)
    assert reg.get("early").preload_order == 10
    assert reg.get("late").preload_order == 20
    assert reg.get("unordered").preload_order is None
    from src.config import load_model_configs

    configs = load_model_configs(yaml_path, apply_overrides=False)
    assert configs["early"]["preload_order"] == 10
    assert configs["late"]["preload_order"] == 20
    assert configs["unordered"]["preload_order"] is None
