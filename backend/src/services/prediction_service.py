"""统一「预测(prediction)」契约纯逻辑(服务层 API spec 2026-06-03,PR-2)。

- `apply_inputs_to_snapshot`:把请求 input 按 exposed_inputs 注入冻结快照副本 —— 旧 `/instances/run`
  **丢弃了 req.inputs**(发布的工作流服务等于不可参数化),这里补上。
- `task_to_prediction`:ExecutionTask → Cog 形 Prediction 对象(`{id,service,status,input,output,...}`)。
"""
from __future__ import annotations

import copy
from typing import Any

# ExecutionTask.status → Prediction.status(Cog 形:starting/processing/succeeded/failed/canceled)。
_STATUS_MAP = {
    "queued": "starting",
    "running": "processing",
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "canceled",
}
TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}


def _input_key(e: dict) -> str | None:
    return e.get("key") or e.get("api_name") or e.get("input_name") or e.get("param_key")


def _find_node(nodes: Any, node_id: Any) -> dict | None:
    """node_id → node dict。兼容 snapshot 两种形:{"nodes": {id: {...}}} / {"nodes": [{id,...}]}。"""
    if isinstance(nodes, dict):
        return nodes.get(str(node_id)) or nodes.get(node_id)
    if isinstance(nodes, list):
        for n in nodes:
            if isinstance(n, dict) and str(n.get("id")) == str(node_id):
                return n
    return None


def apply_inputs_to_snapshot(snapshot: dict, exposed_inputs: list, inputs: dict | None) -> dict:
    """把 `inputs{key:value}` 按 `exposed_inputs`(key→node_id+input_name)注入快照**副本**。

    未在 inputs 里出现的 key 保持快照原值(发布时冻结的默认)。深拷贝,不改原快照。
    """
    snap = copy.deepcopy(snapshot or {})
    inputs = inputs or {}
    nodes = snap.get("nodes")
    for e in (exposed_inputs or []):
        key = _input_key(e)
        if key is None or key not in inputs:
            continue
        node = _find_node(nodes, e.get("node_id"))
        if not isinstance(node, dict):
            continue
        iname = e.get("input_name") or e.get("param_key")
        if not iname:
            continue
        # 快照用 "inputs";编辑形节点用 "data"。优先写已存在的那个,否则建 inputs。
        target = node.get("inputs")
        if not isinstance(target, dict):
            target = node.get("data")
        if not isinstance(target, dict):
            node["inputs"] = {}
            target = node["inputs"]
        target[iname] = inputs[key]
    return snap


def snapshot_to_executor_form(snapshot: dict) -> dict:
    """发布快照 api-shape(nodes=**dict** keyed by node_id,{class_type,inputs,_meta})→ WorkflowExecutor
    吃的编辑形(nodes=**list** [{id,type,data,meta}])。

    发布存 comfy api-shape(workflow_publish._build_snapshot),但 WorkflowExecutor 读 list + `n["id"]`
    —— 两形不通。旧 /run 直接把 dict 快照喂 executor 会崩(无真消费者所以一直没暴露)。这里补转换。
    已是 list(编辑形)则原样返回。
    """
    nodes = snapshot.get("nodes")
    if isinstance(nodes, list):
        return snapshot
    if not isinstance(nodes, dict):
        return {**snapshot, "nodes": []}
    node_list = []
    for nid, n in nodes.items():
        if not isinstance(n, dict):
            continue
        data = n.get("inputs")
        if not isinstance(data, dict):
            data = n.get("data") if isinstance(n.get("data"), dict) else {}
        node_list.append({
            "id": nid,
            "type": n.get("class_type") or n.get("type"),
            "data": data,
            "meta": n.get("_meta") or n.get("meta", {}),
        })
    return {**snapshot, "nodes": node_list}


def prediction_status(task_status: str | None) -> str:
    return _STATUS_MAP.get(task_status or "", "processing")


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def validate_webhook_url(url: str | None) -> str | None:
    """SSRF 防护:校验用户提供的 webhook URL。返回错误串(拒绝)或 None(放行)。

    任何 API-key 持有者都能设 webhook URL,后端会向它发 POST。不校验 → 攻击者让
    后端向内网/云元数据(169.254.169.254)/回环发请求 = 从可信 origin 内网穿透。
    - 只允许 http/https。
    - 解析主机名(getaddrinfo,含 IP 字面量),**任一**解析到的地址落在
      私网/回环/链路本地/保留/未指定/多播 → 拒绝(防 DNS rebinding:逐个 IP 查)。
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    if not url:
        return "webhook url 为空"
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return f"webhook url 解析失败: {url!r}"
    if parsed.scheme not in ("http", "https"):
        return f"webhook 仅允许 http/https,收到 {parsed.scheme!r}"
    host = parsed.hostname
    if not host:
        return "webhook url 缺少主机名"
    try:
        infos = socket.getaddrinfo(host, parsed.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return f"webhook 主机解析失败: {e}"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return f"webhook 解析出非法地址: {ip_str}"
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_unspecified or ip.is_multicast):
            return f"webhook 目标地址不允许(私网/回环/元数据): {ip_str}"
    return None


async def fire_webhook(url: str | None, events_filter: Any, event: str, prediction: dict) -> None:
    """POST 整个 Prediction 对象到 webhook URL(对齐 Cog:payload = prediction 资源本身)。

    `events_filter` 非空时只发被选事件(start/completed/…)。best-effort —— 失败只记不抛(绝不崩 runner)。
    """
    if not url:
        return
    if events_filter and event not in events_filter:
        return
    import logging  # noqa: PLC0415
    _log = logging.getLogger(__name__)
    ssrf_err = validate_webhook_url(url)
    if ssrf_err:
        _log.warning("拒绝 webhook(SSRF 防护): %s —— %s", url, ssrf_err)
        return
    try:
        import httpx  # noqa: PLC0415
        # follow_redirects=False:否则 302 → 内网可绕过上面的地址校验。
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            await client.post(url, json={"event": event, "prediction": prediction})
    except Exception as e:  # noqa: BLE001 — webhook 投递 best-effort
        _log.warning("webhook POST %s failed: %s", url, e)


def task_to_prediction(task, *, service: str | None = None, input_values: Any = None) -> dict:
    """ExecutionTask → Prediction 对象(Cog 形)。

    output 仅在 succeeded 时给(task.result);input 优先用传入值,否则取持久化的 input_json。
    """
    status = prediction_status(getattr(task, "status", None))
    duration_ms = getattr(task, "duration_ms", None)
    persisted_input = getattr(task, "input_json", None)
    render_progress = None
    if status == "processing":
        from src.services.nodes.comfy_bridge import get_comfy_progress

        render_progress = get_comfy_progress(task.id)
    return {
        "id": str(task.id),
        "service": service or getattr(task, "workflow_name", None) or None,
        "status": status,
        "input": input_values if input_values is not None else (persisted_input or {}),
        "output": getattr(task, "result", None) if status == "succeeded" else None,
        "error": getattr(task, "error", None),
        "progress": {
            "nodes_done": getattr(task, "nodes_done", 0) or 0,
            "nodes_total": getattr(task, "nodes_total", 0) or 0,
            **({"comfy": render_progress} if render_progress is not None else {}),
        },
        "metrics": {"predict_time": round(duration_ms / 1000.0, 3)} if duration_ms else {},
        "created_at": _iso(getattr(task, "created_at", None)),
        "started_at": _iso(getattr(task, "started_at", None)),
        "completed_at": _iso(getattr(task, "finished_at", None)),
    }
