from src.services.comfy.usage import summarize_comfy_usage


def test_counts_observed_provider_nodes_and_omits_cached_nodes():
    graph = {
        "qwen": {"inputs": {
            "api_url": "http://127.0.0.1:8000/v1/chat/completions",
            "model_select": "nous-qwen",
        }},
        "deepseek": {"inputs": {"api_url": "https://api.deepseek.com/chat/completions"}},
        "switch": {"class_type": "ComfySwitchNode", "inputs": {
            "switch": True, "on_true": ["deepseek", 0], "on_false": ["qwen", 0],
        }},
        "text": {"inputs": {"llm_service_connector": ["qwen", 0]}},
        "image": {"inputs": {"llm_service_connector": ["switch", 0]}},
        "cached": {"inputs": {"llm_service_connector": ["qwen", 0]}},
        "unused": {"inputs": {"llm_service_connector": ["deepseek", 0]}},
    }
    history = {"status": {"messages": [
        ["execution_start", {"timestamp": 1300}],
        ["execution_cached", {"nodes": ["cached"]}],
        ["execution_success", {"timestamp": 1900}],
    ]}}
    assert summarize_comfy_usage(history, graph, {"text", "image", "cached"}) == {
        "duration_ms": 600,
        "llm_nodes": {"local_qwen": 1, "deepseek": 1},
    }
    assert summarize_comfy_usage(history, graph, set())["llm_nodes"] is None
