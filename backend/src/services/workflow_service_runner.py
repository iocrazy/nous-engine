"""Shared execution core for published workflow services.

Both `/v1/apps/{name}/run` (apps.py) and `/v1/images/generations`
(openai_compat.py) freeze a published workflow into a `ServiceInstance`,
merge caller inputs into the exposed nodes, run the `WorkflowExecutor`
with `runner_clients` injected, and charge 1 call against the
(key, service) grant.

Keeping that core in one place stops the two entrypoints from drifting:
the #339 regression was exactly apps.py forgetting the `runner_clients`
injection that `workflows.py` already had, so any GPU-node workflow
(image/tts/seedvr2) crashed only on the external path. New entrypoints
must go through here, not re-implement the executor wiring.
"""

import time

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.execution_task import ExecutionTask
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance

# Flat input key → the node's primary data slot. The publish dialog used
# to hard-code input_name='value' for everything, but e.g. text_input
# reads data.text — so we also write the type's primary slot, otherwise
# the merge silently misses and the node returns its frozen snapshot
# value (the LLM answers the OLD baked-in prompt, not the caller's).
NODE_PRIMARY_SLOT = {
    "text_input": "text",
    "text_output": "text",
    "multimodal_input": "text",
    "reference_audio": "audio",
    "image_input": "image",
}


def _normalize_nodes(snapshot: dict) -> list:
    """workflow_publish.py freezes nodes as a dict keyed by id (api-shape),
    while the live editor passes a list. The executor only understands the
    list shape, so normalize back to a list of {id,type,data}."""
    raw_nodes = snapshot.get("nodes", [])
    if isinstance(raw_nodes, dict):
        return [
            {
                "id": nid,
                "type": (n.get("class_type") if isinstance(n, dict) else None) or n.get("type"),
                "data": (n.get("inputs") if isinstance(n, dict) else None) or n.get("data") or {},
            }
            for nid, n in raw_nodes.items()
        ]
    return [dict(n) for n in raw_nodes]


def _merge_inputs(nodes: list, exposed_inputs: list | None, inputs: dict) -> None:
    """Merge caller `inputs` (flat {key: value}) into the matching exposed
    nodes' data. Supports v3 (key/input_name) + pre-v3 (api_name/param_key)
    field names. Writes both the declared slot and the node's primary slot."""
    for param in (exposed_inputs or []):
        api_name = param.get("key") or param.get("api_name")
        node_id = param.get("node_id")
        slot = param.get("input_name") or param.get("param_key")
        if api_name is None or node_id is None or slot is None:
            continue
        if api_name not in inputs:
            continue
        for node in nodes:
            if str(node.get("id")) != str(node_id):
                continue
            data = node.setdefault("data", {})
            data[slot] = inputs[api_name]
            primary = NODE_PRIMARY_SLOT.get(str(node.get("type") or "").lower())
            if primary and primary != slot:
                data[primary] = inputs[api_name]


# 图像采样节点 —— 外部 API n / batch 把 num_images 设在这里。
_BATCH_SAMPLER_TYPES = {"flux2_ksampler", "image_generate"}


def _inject_num_images(nodes: list, edges: list, exposed_outputs: list | None, num_images: int) -> None:
    """把 num_images 设到「喂进输出的那个采样节点」(对齐 OpenAI n = 一次出 N 张)。

    从 exposed output(没有则所有 image_output)上溯,命中的**最近**采样节点设 num_images 即返回 ——
    多段链(写真双采+Flux2)只 batch 末段,避免给每段都设 → N^段 爆炸。单段 = 唯一采样节点。
    """
    if num_images <= 1:
        return
    by_id = {str(n.get("id")): n for n in nodes}
    incoming: dict[str, list[str]] = {}
    for e in (edges or []):
        incoming.setdefault(str(e.get("target")), []).append(str(e.get("source")))
    starts = [str(p.get("node_id")) for p in (exposed_outputs or [])
              if isinstance(p, dict) and p.get("node_id") is not None]
    if not starts:
        starts = [str(n.get("id")) for n in nodes
                  if str(n.get("type") or "").lower() == "image_output"]
    seen: set[str] = set()
    queue = list(starts)
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        node = by_id.get(cur)
        if node and str(node.get("type") or "").lower() in _BATCH_SAMPLER_TYPES:
            node.setdefault("data", {})["num_images"] = num_images
            return  # 最近采样节点已设,完成
        queue.extend(incoming.get(cur, []))


async def run_published_workflow(
    request: Request,
    session: AsyncSession,
    svc: ServiceInstance,
    inputs: dict,
    api_key: InstanceApiKey | None,
    num_images: int = 1,
) -> dict:
    """Execute a published workflow service end to end.

    Returns the executor result ({"outputs": {node_id: {...}}}). Charges
    1 call against the (api_key, svc) grant unless `api_key` is None (admin
    path skips quota). The caller is responsible for resolving `svc` with
    workflow_snapshot / exposed_inputs / exposed_outputs undeferred.

    Raises HTTPException(410/403) for retired/paused services and
    HTTPException(500) on execution failure.
    """
    from src.services.quota_gate import NoActiveGrant, consume_for_request
    from src.services.comfy.lora_options import LORA_LIST_TYPE, validate_lora_inputs
    from src.services.resource_pack import QuotaExhausted
    from src.services.workflow_executor import ExecutionError, WorkflowExecutor

    if svc.status == "retired":
        raise HTTPException(410, detail=f"Service '{svc.name}' is retired")
    if svc.status == "paused":
        raise HTTPException(403, detail=f"Service '{svc.name}' is paused")
    lora_keys = [p["key"] for p in svc.exposed_inputs or [] if p.get("type") == LORA_LIST_TYPE]
    lora_errors = validate_lora_inputs(lora_keys, inputs)
    if lora_errors:
        raise HTTPException(422, detail=lora_errors)
    # `deprecated` still serves (per v3 lifecycle spec).

    # 推理前拦已耗尽配额的 key(安全 P2)—— 否则工作流会先跑完 GPU 重活才在下面 best-effort
    # 结算时发现超额并静默吞掉。admin path(api_key None)无 key 可扣,跳过。
    if api_key is not None:
        from src.services.quota_gate import preflight_check
        try:
            await preflight_check(session, api_key_id=api_key.id, service_id=svc.id)
        except QuotaExhausted:
            raise HTTPException(402, detail=f"quota exhausted for service '{svc.name}'")

    snapshot = dict(svc.workflow_snapshot or {})
    nodes = _normalize_nodes(snapshot)
    edges = snapshot.get("edges", [])
    _merge_inputs(nodes, svc.exposed_inputs, inputs)
    # 外部 API n / batch:一次出 N 张(设到喂输出的末段采样节点)。
    _inject_num_images(nodes, edges, svc.exposed_outputs, num_images)

    task = ExecutionTask(
        workflow_name=svc.name,
        service_id=svc.id,
        api_key_id=api_key.id if api_key is not None else None,
        workflow_id=svc.workflow_id,
        input_json=inputs,
        status="running",
        nodes_total=len(nodes),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    start = time.monotonic()
    # Lane K: inject runner_clients (group_id → RunnerClient) so GPU nodes
    # (image/tts/seedvr2) reach their runner subprocess. Omitting this is
    # the #339 regression — keep it identical to workflows.py.
    runner_client = getattr(request.app.state, "runner_client", None)
    runner_clients = getattr(request.app.state, "runner_clients", None)
    executor = WorkflowExecutor(
        {"nodes": nodes, "edges": edges},
        runner_client=runner_client,
        runner_clients=runner_clients,
        task_id=task.id,
    )
    try:
        result = await executor.execute()
        elapsed = int((time.monotonic() - start) * 1000)
        task.status = "completed"
        task.result = result
        task.duration_ms = elapsed
        task.nodes_done = len(nodes)
        task.current_node = None
    except ExecutionError as e:
        elapsed = int((time.monotonic() - start) * 1000)
        task.status = "failed"
        task.error = str(e)
        task.duration_ms = elapsed
        await session.commit()
        raise HTTPException(500, str(e))

    await session.commit()

    # Quota: 1 call per request. Failure here is non-fatal — the work
    # already happened — but exhaustion blocks the next call. Admin path
    # (api_key is None) skips quota entirely (no key to charge).
    if api_key is not None:
        try:
            # allow_overshoot(H1):工作已交付,即使额度被并发抢光也强制记这 1 次
            # (扣成负,下个请求被 preflight 挡)。旧代码在这里吞 QuotaExhausted →
            # 输给 CAS 竞争的并发请求拿到免费未计费推理。现在只有无 pack(无限量)
            # grant 才会走到 except。
            await consume_for_request(
                session, api_key_id=api_key.id, service_id=svc.id, units=1,
                allow_overshoot=True,
            )
            await session.commit()
        except (NoActiveGrant, QuotaExhausted):
            pass

    return result
