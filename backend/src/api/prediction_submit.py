"""提交已发布工作流服务的一次 prediction(spec 2026-09-26 skill-runs §4)。

`/v1/services/{name}/predictions` 与 `/v1/skill-runs/generate` 共用:schema 校验 → 动态
enum → 注入快照 → ExecutionTask → 同步/异步等待 → Prediction 对象,只此一份实现。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.execution_task import ExecutionTask
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance
from src.services.comfy.style_options import resolve_dynamic_enums
from src.services.prediction_service import (
    apply_inputs_to_snapshot,
    snapshot_to_executor_form,
    task_to_prediction,
)
from src.services.service_schema import build_service_io_schema, validate_service_input

# 同步默认上限(秒):无 Prefer 时阻塞,但封顶避免无限挂(长任务用 respond-async)。
_SYNC_CAP_SECONDS = 600.0
_SUBMITTABLE_SOURCE_TYPES = ("workflow", "comfy_template")


def parse_prefer(prefer: str | None) -> tuple[bool, float | None]:
    """Prefer 头 → (async_mode, wait_seconds)。respond-async → 异步;wait=N → 阻塞 N 秒;否则同步。"""
    p = (prefer or "").lower().replace(" ", "")
    if "respond-async" in p:
        return True, None
    if "wait=" in p:
        try:
            return False, float(p.split("wait=", 1)[1].split(",")[0])
        except (ValueError, IndexError):
            return False, None
    return False, None


def check_submittable(instance: ServiceInstance) -> None:
    """只有工作流类服务能提交 prediction;model(LLM)走 chat。"""
    if instance.source_type == "model":
        raise HTTPException(400, detail="model(LLM)服务请用 /v1/chat/completions")
    if instance.source_type not in _SUBMITTABLE_SOURCE_TYPES:
        raise HTTPException(
            400, detail=f"source_type {instance.source_type!r} 暂不支持 predictions")


@dataclass(frozen=True)
class SubmitResult:
    status_code: int
    prediction: dict[str, Any]


async def submit_prediction(
    session: AsyncSession,
    *,
    app_state: Any,
    instance: ServiceInstance,
    api_key: InstanceApiKey | None,
    inputs: dict[str, Any],
    prefer: str | None,
    webhook: str | None = None,
    webhook_events_filter: list[str] | None = None,
) -> SubmitResult:
    """跑一个已发布 workflow 服务(调用方已完成鉴权与服务解析)。"""
    check_submittable(instance)
    snapshot = instance.workflow_snapshot or {}
    if not (snapshot.get("nodes")):
        raise HTTPException(400, detail="service workflow has no nodes")

    # 类型校验(PR-1):按 per-service input schema 校验请求 input。
    schema = build_service_io_schema(
        instance.exposed_inputs, instance.exposed_outputs, snapshot)
    # 选项依赖(x-options-source):某些字段的合法值域取决于**另一个入参的当前值**
    # (krea2:`styles` 的清单随 `style_pack` 变),运行期去 sidecar 取(带 10 分钟
    # 进程内缓存)。在这里**预取**再传给同步的 validate_service_input —— 校验函数本身
    # 保持无 I/O、可单测(理由详见 style_options.resolve_dynamic_enums)。没有字段声明
    # 依赖时这是一次纯字典遍历,不产生任何网络往返。
    dynamic_enums = await resolve_dynamic_enums(schema["input_schema"], inputs)
    errors = validate_service_input(
        schema["input_schema"], inputs, dynamic_enums=dynamic_enums)
    if errors:
        raise HTTPException(422, detail={"message": "input validation failed", "errors": errors})

    # 注入 inputs 到快照副本(PR-2 补:旧 /run 丢弃 inputs)→ 再转 executor 吃的编辑形
    # (发布存 api-shape dict-of-nodes,executor 要 list,旧 /run 没转直接崩,无消费者没暴露)。
    patched = apply_inputs_to_snapshot(snapshot, instance.exposed_inputs, inputs)
    patched = snapshot_to_executor_form(patched)

    task = ExecutionTask(
        workflow_id=instance.source_id,
        # 归属:by-id 端点据此校验 owner(IDOR 防护)。admin session 旁路(Task 10)无
        # key 可归属 —— NULL,与 apps.py 的 admin_run 一致(admin 隐式对所有 prediction 有权限,
        # get/cancel_prediction 对 api_key is None 的调用方跳过 owner 校验)。
        api_key_id=api_key.id if api_key is not None else None,
        workflow_name=instance.name,
        status="queued",
        nodes_total=len(patched.get("nodes") or []),
        input_json=inputs,
        webhook_url=webhook,
        webhook_events=webhook_events_filter,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    if api_key is not None:
        # 用量计数(原子自增)。完整配额消费收敛到 PR-5。admin 路径无 key 可计,跳过
        # (镜像 apps.py::execute_service 的 admin_run 分支:admin 不耗配额)。
        await session.execute(
            update(InstanceApiKey).where(InstanceApiKey.id == api_key.id)
            .values(usage_calls=InstanceApiKey.usage_calls + 1))
        await session.commit()

    from src.services.workflow_runner import run_workflow_task  # noqa: PLC0415
    runner_client = getattr(app_state, "runner_client", None)
    runner_clients = getattr(app_state, "runner_clients", None)
    exec_coro = run_workflow_task(
        task.id, patched, runner_client=runner_client,
        runner_clients=runner_clients, channel_id=str(instance.id))
    exec_task = asyncio.create_task(exec_coro)

    async_mode, wait_seconds = parse_prefer(prefer)
    status_code = 200
    if async_mode:
        status_code = 202
    else:
        # 同步 / wait=N:shield 防超时取消执行;超时则返当前态(任务后台继续)。
        timeout = wait_seconds if wait_seconds is not None else _SYNC_CAP_SECONDS
        try:
            await asyncio.wait_for(asyncio.shield(exec_task), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        await session.refresh(task)

    return SubmitResult(
        status_code=status_code,
        prediction=task_to_prediction(task, service=instance.name, input_values=inputs))
