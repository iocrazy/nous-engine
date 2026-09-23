"""服务能力推导(services/model_capabilities.py)+ `GET /api/v1/services` 的 capabilities 字段。

全部用构造的 cfg dict / tmp_path 假模型目录,不读真模型目录、不碰 GPU。
"""
from __future__ import annotations

import json

import pytest

from src.api.response_cache import invalidate
from src.services.model_capabilities import derive_capabilities


def _llm_cfg(**params) -> dict:
    return {
        "name": "cap_llm",
        "type": "llm",
        "paths": {"main": "llm/Cap-LLM"},
        "source": "huihui-ai/Huihui-Qwen3.8-27B-abliterated",
        "params": params,
    }


@pytest.fixture
def models_root(tmp_path, monkeypatch):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "LOCAL_MODELS_PATH", str(tmp_path))
    return tmp_path


def _write_config(root, rel: str, config: dict) -> None:
    d = root / rel
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(config))


def test_llm_tools_thinking_context_provider(models_root):
    caps = derive_capabilities(_llm_cfg(
        max_model_len=32768,
        vllm_args={
            "reasoning-parser": "qwen3",
            "enable-auto-tool-choice": True,
            "tool-call-parser": "qwen3_xml",
        },
    ))
    assert caps == {
        "context": 32768,
        "max_output": None,
        "tools": True,
        "thinking": True,
        "vision": False,  # 没有 config.json → 读不到 → False
        "provider": "huihui-ai",
        "source": "huihui-ai/Huihui-Qwen3.8-27B-abliterated",
    }


def test_vllm_args_underscore_keys_and_false_values(models_root):
    caps = derive_capabilities(_llm_cfg(vllm_args={
        "enable_auto_tool_choice": True,
        "reasoning_parser": "qwen3",
    }))
    assert caps["tools"] is True and caps["thinking"] is True

    caps = derive_capabilities(_llm_cfg(vllm_args={"enable-auto-tool-choice": False}))
    assert caps["tools"] is False and caps["thinking"] is False


def test_context_prefers_vllm_args_max_model_len(models_root):
    # merge_vllm_args:同名 flag 以 vllm_args 为准 —— 能力里报的也得是真正生效的那个
    caps = derive_capabilities(_llm_cfg(max_model_len=32768, vllm_args={"max_model_len": 65536}))
    assert caps["context"] == 65536


def test_nothing_derivable_is_none_false():
    caps = derive_capabilities({"name": "cosyvoice2", "type": "tts", "paths": {"main": "x"}})
    assert caps == {
        "context": None, "max_output": None, "tools": False, "thinking": False,
        "vision": False, "provider": None, "source": None,
    }


@pytest.mark.parametrize("config, expected", [
    ({"architectures": ["Qwen3_5ForConditionalGeneration"], "vision_config": {"depth": 27}}, True),
    ({"architectures": ["Qwen2_5_VLForConditionalGeneration"]}, True),
    ({"architectures": ["Qwen3OmniMoeForConditionalGeneration"]}, True),
    ({"architectures": ["Qwen3ForCausalLM"]}, False),
])
def test_llm_vision_from_config_json(models_root, config, expected):
    _write_config(models_root, "llm/Cap-LLM", config)
    assert derive_capabilities(_llm_cfg())["vision"] is expected


def test_understand_type_also_reads_config_json(models_root):
    _write_config(models_root, "ocr/X", {"architectures": ["SomeVisionModel"]})
    cfg = {"name": "ocr", "type": "understand", "paths": {"main": "ocr/X"}}
    assert derive_capabilities(cfg)["vision"] is True


@pytest.mark.parametrize("limit, expected", [
    ({"image": 1, "video": 1}, True),
    ('{"image": 2}', True),  # 透传值也可能写成 JSON 串
    ({"image": 0, "video": 1}, False),
    (None, False),
])
def test_embedding_vision_from_limit_mm(models_root, limit, expected):
    # embedding 不看 config.json(WeMM 的 config 也带 vision_config),只看是否放开了 image 输入
    _write_config(models_root, "embedding/E", {"vision_config": {}})
    vllm_args = {} if limit is None else {"limit-mm-per-prompt": limit}
    cfg = {"name": "e", "type": "embedding", "paths": {"main": "embedding/E"},
           "params": {"max_model_len": 8192, "vllm_args": vllm_args}}
    caps = derive_capabilities(cfg)
    assert caps["vision"] is expected
    assert caps["context"] == 8192


def test_adapter_uses_same_vision_predicate():
    """适配器与能力推导共用 is_vision_model_config —— 判据只有一份。"""
    import inspect

    from src.services.inference import llm_vllm

    src = inspect.getsource(llm_vllm.VLLMAdapter)
    assert "is_vision_model_config(model_config)" in src
    assert '"Multimodal" in a' not in src


@pytest.mark.asyncio
async def test_services_list_includes_capabilities(db_client, monkeypatch, models_root):
    import src.config as config_mod

    fake = {
        "cap_llm": _llm_cfg(
            max_model_len=262144,
            vllm_args={"reasoning-parser": "qwen3", "enable-auto-tool-choice": True},
        ),
    }
    monkeypatch.setattr(config_mod, "load_model_configs", lambda *a, **k: fake)

    r = await db_client.post(
        "/api/v1/services/register-model",
        json={"name": "cap-llm", "source_name": "cap_llm", "type": "llm"},
    )
    assert r.status_code == 201, r.text
    invalidate("services")

    r = await db_client.get("/api/v1/services")
    assert r.status_code == 200, r.text
    svc = next(s for s in r.json() if s["name"] == "cap-llm")
    assert svc["capabilities"] == {
        "context": 262144,
        "max_output": None,
        "tools": True,
        "thinking": True,
        "vision": False,
        "provider": "huihui-ai",
        "source": "huihui-ai/Huihui-Qwen3.8-27B-abliterated",
    }


@pytest.mark.asyncio
async def test_services_list_capabilities_null_for_unknown_engine(db_client, monkeypatch):
    import src.config as config_mod

    monkeypatch.setattr(config_mod, "load_model_configs", lambda *a, **k: {})
    r = await db_client.post(
        "/api/v1/services/register-model",
        json={"name": "ghost-llm", "source_name": "not_in_registry", "type": "llm"},
    )
    assert r.status_code == 201, r.text
    invalidate("services")

    r = await db_client.get("/api/v1/services")
    svc = next(s for s in r.json() if s["name"] == "ghost-llm")
    assert svc["capabilities"] is None


@pytest.mark.asyncio
async def test_launch_params_patch_invalidates_services_cache(db_client, models_root):
    """capabilities.context 就是 max_model_len —— 改了启动参数,服务列表不能再吐 30s 旧缓存。"""
    r = await db_client.post(
        "/api/v1/services/register-model",
        json={"name": "awq-cap", "source_name": "qwen3_8_27b_abliterated_awq", "type": "llm"},
    )
    assert r.status_code == 201, r.text
    invalidate("services")
    await db_client.get("/api/v1/services")  # 填缓存

    r = await db_client.patch(
        "/api/v1/engines/qwen3_8_27b_abliterated_awq/launch-params",
        json={"max_model_len": 12288})
    assert r.status_code == 200, r.text

    r = await db_client.get("/api/v1/services")
    svc = next(s for s in r.json() if s["name"] == "awq-cap")
    assert svc["capabilities"]["context"] == 12288
