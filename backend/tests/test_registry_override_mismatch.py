"""覆盖表静默取消 yaml 的 resident / 落卡声明时必须留痕(2026-09-05 复审 A)。

真机事故:`qwen3_embedding_8b` 的 yaml 写了 `resident: true`,`model_runtime_overrides`
里那行却是 `resident=False, gpu=2`。目录改完看着已生效,机器上常驻根本没起来,
启动日志里一个字都没有。覆盖优先是设计,「悄悄地」不是。
"""
from __future__ import annotations

import logging

import pytest

from src.services.inference.registry import ModelRegistry, _log_override_mismatch


def _yaml_entry(**kw):
    e = {
        "id": "m1",
        "type": "llm",
        "adapter": "src.services.inference.llm_vllm.VLLMAdapter",
        "paths": {"main": "llm/M1"},
        "vram_mb": 1000,
        "params": {},
    }
    e.update(kw)
    return e


def test_resident_true_cancelled_by_override_logs_error(caplog):
    with caplog.at_level(logging.ERROR, logger="src.services.inference.registry"):
        _log_override_mismatch("m1", _yaml_entry(resident=True), {"resident": False})
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs, "yaml resident:true 被覆盖成 False 必须打 error"
    assert "m1" in errs[0].getMessage() and "resident" in errs[0].getMessage()


def test_agreeing_override_is_silent(caplog):
    """覆盖与 yaml 一致(或覆盖也是 true)→ 不该有任何噪音。"""
    with caplog.at_level(logging.WARNING, logger="src.services.inference.registry"):
        _log_override_mismatch("m1", _yaml_entry(resident=True, gpu=1), {"resident": True, "gpu": 1})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_yaml_without_resident_is_not_reported(caplog):
    """yaml 没承诺常驻 → 覆盖成 False 不是「承诺落空」,不报。"""
    with caplog.at_level(logging.WARNING, logger="src.services.inference.registry"):
        _log_override_mismatch("m1", _yaml_entry(), {"resident": False})
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("yaml_kw,ov,needle", [
    ({"gpu": 1}, {"gpu": 2}, "gpu"),
    ({"gpus": [0, 2]}, {"gpus": [1]}, "gpus"),
])
def test_placement_override_logs_warning_not_error(caplog, yaml_kw, ov, needle):
    """换卡是可接受的运维动作 → warning(不像常驻落空那样直接毁显存规划)。"""
    with caplog.at_level(logging.WARNING, logger="src.services.inference.registry"):
        _log_override_mismatch("m1", _yaml_entry(**yaml_kw), ov)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns and needle in warns[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_registry_load_reports_the_mismatch(caplog, monkeypatch):
    """走真正的 `_load` 路径 —— 告警必须挂在 registry 建 spec 的那条线上,不是只在 helper 里。"""
    import src.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "collect_model_entries",
                        lambda _p: [_yaml_entry(resident=True, gpu=1)])
    monkeypatch.setattr(cfg_mod, "load_runtime_overrides",
                        lambda: {"m1": {"resident": False, "gpu": 2}})

    with caplog.at_level(logging.WARNING, logger="src.services.inference.registry"):
        reg = ModelRegistry("configs/models.yaml")

    assert reg.get("m1") is not None and reg.get("m1").resident is False  # 覆盖仍然优先
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("resident" in m for m in msgs), "常驻承诺落空必须 error"
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("gpu" in m for m in warns), "落卡被换必须 warning"
