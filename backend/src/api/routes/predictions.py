"""统一「预测(prediction)」端点(服务层 API spec 2026-06-03,PR-2)。

`POST /api/v1/services/{name}/predictions` —— 一个端点,`Prefer` 头切同步/异步,跑已发布的
**workflow** 服务(对齐 Cog predictions + ComfyUI prompt)。修「工作流不可参数化」(旧 /run 丢 inputs)
+「M:N key 调不了工作流」(旧只 openai_compat 走 model,workflow 501)。

- 默认同步:阻塞至终态。
- `Prefer: respond-async` → 202 + status:processing,客户端轮询 `GET /predictions/{id}`。
- `Prefer: wait=N` → 阻塞至多 N 秒,超时返当前态转轮询。

model(LLM)服务仍走 `/v1/chat/completions`(chat 形,不塞进通用 predictions);preset(TTS)暂留
`/v1/instances/{id}/synthesize`(后续折叠)。本 PR 删旧 `/v1/instances/{id}/run`(被 predictions 取代)。
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_auth import verify_bearer_token_any
from src.api.prediction_submit import parse_prefer, submit_prediction
from src.api.service_access import (
    auth_bearer_or_admin_session as _auth_predictions,
    resolve_service_for_call as _resolve_service,
)
from src.models.database import get_async_session
from src.models.execution_task import ExecutionTask
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance
from src.services.prediction_service import task_to_prediction

_parse_prefer = parse_prefer  # 旧 import 路径兼容(tests/test_prediction_service_pr2.py)

# /v1/* = 对外 bearer-authed 端点(AdminSessionGate 只拦 /api/*,这里用各自的 bearer 校验)。
# 放 /api/v1 会被 admin cookie 门拦死 bearer 客户端(真机 smoke 逮到)。
router = APIRouter(prefix="/v1", tags=["predictions"])


class PredictionRequest(BaseModel):
    input: dict[str, Any] | None = None
    # PR-3:webhook 回调(对齐 Cog)。webhook=完成/开始时 POST Prediction 的 URL;
    # webhook_events_filter=["start","completed",...] 过滤,省略=全发。
    webhook: str | None = None
    webhook_events_filter: list[str] | None = None


@router.get("/services/{name}/schema")
async def get_service_schema(
    name: str,
    session: AsyncSession = Depends(get_async_session),
):
    """per-service I/O JSON-Schema(input/output)—— 机器可发现的调用契约(服务层 API spec PR-1)。

    **公开端点**(第三方集成先拿契约,无 auth),按 service `name` 查。在 /v1(非 /api/v1)避开 admin cookie 门。
    从 exposed_inputs/outputs + 各节点 node.yaml widget 生成(对齐 Cog 声明即 schema + ComfyUI object_info)。
    """
    from sqlalchemy.orm import undefer  # noqa: PLC0415

    from src.services.service_schema import build_service_io_schema  # noqa: PLC0415
    stmt = (
        select(ServiceInstance)
        .options(
            undefer(ServiceInstance.workflow_snapshot),
            undefer(ServiceInstance.exposed_inputs),
            undefer(ServiceInstance.exposed_outputs),
        )
        .where(ServiceInstance.name == name)
    )
    svc = (await session.execute(stmt)).scalar_one_or_none()
    if svc is None:
        raise HTTPException(404, detail="service not found")
    schema = build_service_io_schema(
        svc.exposed_inputs, svc.exposed_outputs, svc.workflow_snapshot)
    return {
        "service": name,
        "category": svc.category,
        "source_type": svc.source_type,
        "input_schema": schema["input_schema"],
        "output_schema": schema["output_schema"],
    }


@router.post("/services/{name}/predictions")
async def create_prediction(
    name: str,
    body: PredictionRequest,
    request: Request,
    response: Response,
    prefer: str | None = Header(default=None),
    auth: tuple[ServiceInstance | None, InstanceApiKey | None] = Depends(_auth_predictions),
    session: AsyncSession = Depends(get_async_session),
):
    """跑一个已发布 workflow 服务,返回 Prediction 对象(Cog 形)。

    Task 7:`comfy_template`(ComfyUI workflow bridge,见 comfy_templates.py)也走这条统一入口
    ——design spec 2026-08-10 §「提交」明确画的就是这条路径。它的 workflow_snapshot 是固定桥
    快照(list 形,单 `comfyui_workflow` 节点 + `video_output` 节点),下面的
    apply_inputs_to_snapshot/snapshot_to_executor_form 对 list 形 snapshot 已经是透传+按
    node_id 注入,天然兼容,不需要专门分支。
    """
    instance, api_key = await _resolve_service(session, auth, name)
    result = await submit_prediction(
        session, app_state=request.app.state, instance=instance, api_key=api_key,
        inputs=body.input or {}, prefer=prefer,
        webhook=body.webhook, webhook_events_filter=body.webhook_events_filter,
    )
    response.status_code = result.status_code
    return result.prediction


@router.get("/predictions/{prediction_id}")
async def get_prediction(
    prediction_id: int,
    auth: tuple[ServiceInstance | None, InstanceApiKey | None] = Depends(_auth_predictions),
    session: AsyncSession = Depends(get_async_session),
):
    """轮询一个 prediction 的状态/结果。"""
    _, api_key = auth
    task = (await session.execute(
        select(ExecutionTask).where(ExecutionTask.id == prediction_id))).scalar_one_or_none()
    # IDOR 防护:by-id 只允许创建它的 key 访问;归属不符对 api-key 调用方一律 404(不泄漏
    # 存在性)。admin session 旁路(`api_key is None`,Task 10)跳过 —— admin 对所有
    # prediction 隐式有权限,与 `/api/v1/tasks/{id}`(admin-only,无 owner 校验)对齐。
    if task is None or (api_key is not None and task.api_key_id != api_key.id):
        raise HTTPException(404, detail="prediction not found")
    return task_to_prediction(task, service=task.workflow_name)


@router.get("/predictions/{prediction_id}/stream")
async def stream_prediction(
    prediction_id: int,
    auth: tuple[ServiceInstance | None, InstanceApiKey] = Depends(verify_bearer_token_any),
):
    """SSE 流:每次状态/进度变化推一条 `data: <prediction>`,终态后结束(对齐 Cog SSE + ComfyUI ws)。

    每轮用独立 session 读最新已提交态(后台执行写的是另一个 session)。404 不存在则发一条 error 即结束。
    """
    import json  # noqa: PLC0415

    from fastapi.responses import StreamingResponse  # noqa: PLC0415

    from src.models.database import get_session_factory  # noqa: PLC0415

    _, api_key = auth

    async def _gen():
        sf = get_session_factory()
        last_sig = None
        while True:
            async with sf() as s:
                task = await s.get(ExecutionTask, prediction_id)
            # IDOR 防护:归属不符 / NULL 对 api-key 调用方一律当"不存在"(同 get_prediction)。
            if task is None or task.api_key_id != api_key.id:
                yield f"event: error\ndata: {json.dumps({'error': 'prediction not found'})}\n\n"
                return
            pred = task_to_prediction(task, service=task.workflow_name)
            sig = (pred["status"], task.nodes_done)
            if sig != last_sig:
                yield f"data: {json.dumps(pred, ensure_ascii=False)}\n\n"
                last_sig = sig
            if pred["status"] in ("succeeded", "failed", "canceled"):
                return
            await asyncio.sleep(0.3)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post("/predictions/{prediction_id}/cancel")
async def cancel_prediction(
    prediction_id: int,
    auth: tuple[ServiceInstance | None, InstanceApiKey | None] = Depends(_auth_predictions),
    session: AsyncSession = Depends(get_async_session),
):
    """取消一个进行中的 prediction(runner 在节点边界检查 status=cancelled 中止)。

    comfy_template 服务的桥节点(Task 6)不经 runner_clients 广播那套 abort —— 它是串行
    跑在这个进程里、正堵在 `ComfyClient.wait()` 上,唯一能真正打断当前渲染的是转发
    ComfyUI 的 `/interrupt`(串行语义下"中断当前"即"中断本任务",局限见 spec §12)。

    C1 fix:一个 comfy_template 服务的 render 信号量(`comfy_bridge._SEM`)是**进程级
    单例**,不是每个 prediction 一把——排在信号量后面等待的其它 comfy_template
    prediction 的 DB 状态也是 "running"(workflow_runner 在拿到信号量之前就已经把
    status 落 running 了)。旧代码只要 status=="running" 就无条件转发 `/interrupt`,
    会把**当前真正在 ComfyUI 上跑的那个不相干任务**打断,取消 B 却误杀 A 在渲染的图。
    现在只在被取消的 prediction_id 确实是 `comfy_bridge` 当前持有信号量的那个任务时
    才转发 —— 排在后面的任务靠桥节点自己在拿到信号量后的 cancel-race 复查跳过渲染
    (comfy_bridge.py::_task_is_cancelled),不需要也不该经这条 interrupt 路径。

    **先落 cancelled、再转发 interrupt**(2026-09-02 修的产品赛跑,CI flake 循着它来):
    旧顺序是 interrupt → 条件 UPDATE。`await interrupt()` 在生产里是一次最长 5s 的真实
    HTTP 往返、在测试里也至少让出一次事件循环 —— 这个窗口里被打断的渲染会一路走完
    `ComfyClient.wait()` 返回 → 桥节点收尾 → workflow_runner 抢先把终态写成
    completed/failed。等 cancel 的条件 UPDATE 才落到 DB 时它只能匹配 queued/running,
    0 行生效,于是:
      · 中断成功(桥节点抛"未产出任何产物")→ runner 看到 status 还是 running → 落
        **failed**,用户点的取消被记成失败;
      · 测试替身那种「interrupt 后 wait 仍返回成功」→ 落 **completed** → 端点回
        `succeeded`(就是 flaky 的那条断言)。
    先把 cancelled 提交,取消意图对后到的 runner 就是可见的既成事实(runner 两条收尾
    路径都 honor cancelled,见 workflow_runner.py),interrupt 只剩"尽快止损"的作用,
    不再参与终态竞速。条件 UPDATE 本身保留:渲染在我们下手**之前**就已经真跑完的
    情况仍然让真实终态赢(0 行生效 → refresh 拿到 completed,不把成功硬翻成取消)。
    """
    _, api_key = auth
    task = (await session.execute(
        select(ExecutionTask).where(ExecutionTask.id == prediction_id))).scalar_one_or_none()
    # IDOR 防护:非 owner 一律 404,防跨租户取消(DoS)。admin session 旁路
    # (`api_key is None`,Task 10)跳过 —— admin 可取消任意 prediction,同 get_prediction。
    if task is None or (api_key is not None and task.api_key_id != api_key.id):
        raise HTTPException(404, detail="prediction not found")
    if task.status in ("completed", "failed", "cancelled"):
        return task_to_prediction(task, service=task.workflow_name)
    # 先*判断*要不要 interrupt(纯读,没有任何副作用),真正的 `await interrupt()` 留到
    # cancelled 落库之后 —— 这一步不会放行任何渲染,所以不参与终态竞速。
    should_interrupt = False
    if task.status == "running":
        svc = (await session.execute(
            select(ServiceInstance).where(ServiceInstance.name == task.workflow_name)
        )).scalar_one_or_none()
        if svc is not None and svc.source_type == "comfy_template":
            from src.services.nodes import comfy_bridge  # noqa: PLC0415
            should_interrupt = comfy_bridge.get_running_task_id() == prediction_id

    # TOCTOU 守护:条件 UPDATE 只在仍是 queued/running 时才真的落 cancelled —— 渲染在上面
    # 那两次 DB 往返期间抢先跑完(completed/failed)时 0 行生效,不把一个已成功的终态硬翻
    # 回 cancelled 且丢 output(task_to_prediction 只在 succeeded 时给 output)。
    marked = (await session.execute(
        update(ExecutionTask).where(
            ExecutionTask.id == prediction_id,
            ExecutionTask.status.in_(("queued", "running")),
        ).values(status="cancelled", cancel_reason="client cancel"))).rowcount
    await session.commit()

    # 渲染已经赢下竞态(marked==0)时不再打断:那一发 `/interrupt` 打不到本任务,只会误伤
    # sidecar 上排在后面的下一个渲染(C1 那条不变式)。落库到这里之间还隔着一次 commit
    # 往返,渲染也可能恰好在这个缝里跑完、下一个任务已拿到信号量 —— 所以发 interrupt 前
    # **再核一次**持有者仍是本任务,把误伤窗口压到最小(纯内存读,零成本)。
    if marked and should_interrupt and comfy_bridge.get_running_task_id() == prediction_id:
        from src.api.routes.comfy_templates import get_client  # noqa: PLC0415
        try:
            await get_client().interrupt()
        except Exception as e:  # noqa: BLE001 — sidecar 掉线不该挡住已落的 cancelled
            import logging  # noqa: PLC0415
            logging.getLogger(__name__).warning(
                "cancel_prediction: comfy interrupt failed for task %s: %s",
                prediction_id, e)
    await session.refresh(task)
    return task_to_prediction(task, service=task.workflow_name)
