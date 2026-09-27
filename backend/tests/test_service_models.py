"""Unit tests for service_models.extract_service_models (display-only ref
enumeration for the service overview)."""
from src.services.service_models import extract_service_models


def _published(nodes: dict) -> dict:
    return {"schema": "comfy/api-1", "nodes": nodes}


def test_llm_and_tts_engines():
    snap = _published({
        "n1": {"class_type": "llm", "inputs": {"model_key": "qwen3-8b"}},
        "n2": {"class_type": "tts_engine", "inputs": {"engine": "cosyvoice2"}},
    })
    refs = extract_service_models(snap)
    by_key = {r["engine_key"]: r for r in refs}
    assert by_key["qwen3-8b"]["kind"] == "engine"
    assert by_key["qwen3-8b"]["role"] == "llm"
    assert by_key["cosyvoice2"]["role"] == "tts"


def test_trivial_quick_provision_engine():
    # quick-provision uses class_type "LLMEngine"/"TTSEngine" + inputs.engine
    snap = _published({
        "engine_1": {"class_type": "LLMEngine", "inputs": {"engine": "qwen3-5"}},
    })
    refs = extract_service_models(snap)
    assert len(refs) == 1
    assert refs[0]["kind"] == "engine"
    assert refs[0]["engine_key"] == "qwen3-5"


def test_image_generate_engine_role_is_not_llm():
    """Regression: the legacy integrated image_generate node carries the image
    engine via `model_key` — the same param name the llm node uses. Before the
    fix it fell into the generic `"model_key" in inp` branch and got role="llm",
    so a Flux2 image engine showed up as an LLM in the service overview."""
    snap = _published({
        "gen": {"class_type": "image_generate",
                "inputs": {"model_key": "flux2-klein-9b-true-v2-fp8mixed"}},
    })
    refs = extract_service_models(snap)
    assert len(refs) == 1
    assert refs[0]["kind"] == "engine"
    assert refs[0]["engine_key"] == "flux2-klein-9b-true-v2-fp8mixed"
    assert refs[0]["role"] == "diffusion_models"  # NOT "llm"


def test_empty_and_malformed_snapshots():
    assert extract_service_models(None) == []
    assert extract_service_models({}) == []
    assert extract_service_models({"nodes": {}}) == []
    assert extract_service_models({"nodes": [{"id": "x"}]}) == []


def test_dedup_same_engine_across_nodes():
    # 替代已删的 test_dedup_same_file_across_nodes(其 flux2 组件节点随自建图像引擎删除):
    # 去重语义本身保留,改用 engine 引用覆盖。
    snap = _published({
        "a": {"class_type": "llm", "inputs": {"model_key": "qwen3-8b"}},
        "b": {"class_type": "llm", "inputs": {"model_key": "qwen3-8b"}},
    })
    assert len(extract_service_models(snap)) == 1


def test_editor_shape_list_with_type_data_engine():
    # 替代已删的 test_editor_shape_list_with_type_data:编辑器形状({id,type,data} 列表)
    # 的兼容性保留,改用 engine 节点覆盖。
    snap = {"nodes": [
        {"id": "n1", "type": "tts_engine", "data": {"engine": "cosyvoice2"}},
    ]}
    refs = extract_service_models(snap)
    assert len(refs) == 1 and refs[0]["role"] == "tts"


def test_flux2_component_nodes_no_longer_emit_refs():
    # 自建图像引擎删除后,旧快照里残留的 flux2 组件加载节点不再产出任何引用。
    snap = _published({
        "n1": {"class_type": "flux2_load_diffusion_model", "inputs": {"file": "/m/x.safetensors"}},
        "n2": {"class_type": "flux2_load_clip", "inputs": {"clips": [{"file": "/m/c.safetensors"}]}},
        "n3": {"class_type": "flux2_load_vae", "inputs": {"file": "/m/v.safetensors"}},
    })
    assert extract_service_models(snap) == []
