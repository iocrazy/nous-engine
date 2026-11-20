from __future__ import annotations

from urllib.parse import urlsplit


def _linked_id(value) -> str | None:
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)):
        return str(value[0])
    return None


def _provider_for(graph: dict, connector) -> str | None:
    visited = set()
    while (node_id := _linked_id(connector)) is not None and node_id not in visited:
        visited.add(node_id)
        node = graph.get(node_id, {})
        inputs = node.get("inputs", {})
        if node.get("class_type") != "ComfySwitchNode":
            if node.get("class_type") == "SetDeepSeekLLMServiceConnector|Mie":
                return "deepseek"
            url = inputs.get("api_url")
            if not isinstance(url, str):
                return None
            try:
                parsed = urlsplit(url)
                if parsed.hostname == "api.deepseek.com":
                    return "deepseek"
                if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.path == "/v1/chat/completions":
                    model = inputs.get("model_select") or inputs.get("model")
                    if isinstance(model, str) and "qwen" in model.lower():
                        return "local_qwen"
            except ValueError:
                pass
            return None

        switch = inputs.get("switch")
        if (switch_id := _linked_id(switch)) is not None:
            switch_node = graph.get(switch_id, {})
            switch = switch_node.get("inputs", {}).get("value") if switch_node.get("class_type") == "PrimitiveBoolean" else None
        if not isinstance(switch, bool):
            return None
        connector = inputs.get("on_true" if switch else "on_false")
    return None


def summarize_comfy_usage(history: dict, graph: dict, observed_nodes: set[str]) -> dict:
    status = history.get("status") or {}
    started = ended = None
    cached_nodes: set[str] = set()
    executed_nodes = set(observed_nodes)
    observation_available = bool(observed_nodes)
    for message in status.get("messages") or []:
        if not isinstance(message, (list, tuple)) or len(message) != 2 or not isinstance(message[1], dict):
            continue
        event, data = message
        timestamp = data.get("timestamp")
        if event == "execution_start" and isinstance(timestamp, int):
            started = timestamp
        if event in {"execution_success", "execution_error", "execution_interrupted"}:
            if isinstance(timestamp, int):
                ended = timestamp
            executed = data.get("executed")
            if isinstance(executed, list):
                executed_nodes.update(map(str, executed))
                observation_available = True
        if event == "execution_cached":
            cached_nodes.update(map(str, data.get("nodes") or []))

    duration = ended - started if started is not None and ended is not None and ended >= started else None
    llm_nodes = {"local_qwen": 0, "deepseek": 0}
    for node_id in executed_nodes - cached_nodes:
        connector = graph.get(node_id, {}).get("inputs", {}).get("llm_service_connector")
        provider = _provider_for(graph, connector)
        if provider is not None:
            llm_nodes[provider] += 1
    return {"duration_ms": duration, "llm_nodes": llm_nodes if observation_available else None}
