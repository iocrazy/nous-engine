import base64
import binascii
from io import BytesIO
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from anyio import to_thread
from sqlalchemy import case, select, desc, func
from sqlalchemy.orm import with_expression
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.database import get_async_session
from src.models.execution_task import ExecutionTask
from src.utils.constants import VALID_TASK_STATUSES
from src.api.websocket import ws_manager
from src.api.routes.image_files import preview_limiter, render_preview
# 序列化器下沉到 services 层(打破 workflow_runner→本模块 的反向依赖)。reaper
# collect_referenced_image_uuids 仍在本文件(用 session),向下 import 抽 url 的纯 helper。
from src.services.execution_task_serialize import (  # noqa: F401
    _detect_asr_meta,
    _detect_image_meta,
    _detect_llm_meta,
    _detect_tts_meta,
    _detect_vision_meta,
    _image_urls,
    _iter_node_outputs,
    _task_to_dict,
    _uuid_from_image_url,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tasks", tags=["execution-tasks"])


@router.get("")
async def list_tasks(
    limit: int = 20,
    offset: int = 0,
    status: str | None = None,
    workflow_id: str | None = None,
    include_input: bool = True,
    session: AsyncSession = Depends(get_async_session),
):
    # workflow_id 过滤要在 limit/offset 之前 where,否则先截断再过滤会漏。
    stmt = select(ExecutionTask).order_by(desc(ExecutionTask.created_at))
    if status:
        stmt = stmt.where(ExecutionTask.status == status)
    # 服务详情「用量/历史」tab:按源 workflow 归属过滤(service run 的 task 经 PR-A 已带
    # workflow_id)。snowflake id 是 str,强转 int;非法值不抛、直接返回空集。
    if workflow_id:
        try:
            stmt = stmt.where(ExecutionTask.workflow_id == int(workflow_id))
        except (TypeError, ValueError):
            return []
    if not include_input:
        params = ExecutionTask.input_json
        asr_parameters = case((
            ExecutionTask.workflow_id.is_(None),
            func.json_build_object("kind", params["kind"], "audio_seconds", params["audio_seconds"]),
        ))
        stmt = stmt.options(with_expression(params, asr_parameters))
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    tasks = result.scalars().all()
    return [_task_to_dict(t, include_input=include_input) for t in tasks]


def _video_url(result: object) -> str | None:
    for node in _iter_node_outputs(result):
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("video_url"), str) and node["video_url"]:
            return node["video_url"]
        for item in node.get("items") or []:
            if isinstance(item, dict) and item.get("kind") == "video":
                url = item.get("url")
                if isinstance(url, str) and url:
                    return url
    return None


@router.get("/gallery")
async def list_gallery(
    limit: int = Query(24, ge=1, le=48),
    before: int | None = Query(None, ge=1, le=2**63 - 1),
    session: AsyncSession = Depends(get_async_session),
):
    t = ExecutionTask
    scan_limit = limit * 4
    stmt = (
        select(t.id, t.workflow_id, t.workflow_name, t.created_at, t.duration_ms, t.result)
        .where(t.status == "completed", t.result.is_not(None))
        .order_by(t.id.desc())
        .limit(scan_limit + 1)
    )
    if before is not None:
        stmt = stmt.where(t.id < before)
    rows = (await session.execute(stmt)).all()
    items = []
    next_before = None
    for index, row in enumerate(rows[:scan_limit]):
        urls = _image_urls(row.result)
        if urls:
            items.append({
                "id": str(row.id),
                "workflow_id": str(row.workflow_id) if row.workflow_id else None,
                "workflow_name": row.workflow_name,
                "created_at": row.created_at.isoformat(),
                "duration_ms": row.duration_ms,
                "output_thumbnails": [
                    f"/api/v1/tasks/gallery/{row.id}/image/{slot}" if url.startswith("data:") else url
                    for slot, url in enumerate(urls)
                ],
                "video_url": _video_url(row.result),
            })
        if index + 1 < len(rows):
            next_before = str(row.id)
        else:
            next_before = None
        if len(items) == limit:
            break
    return {"items": items, "next_before": next_before}


@router.get("/gallery/{task_id}/image/{slot}")
async def get_gallery_image(
    task_id: int,
    slot: int,
    preview: bool = False,
    session: AsyncSession = Depends(get_async_session),
):
    result = await session.scalar(select(ExecutionTask.result).where(ExecutionTask.id == task_id))
    urls = _image_urls(result)
    if not 0 <= slot < len(urls):
        raise HTTPException(404, "Image not found")
    header, _, payload = urls[slot].partition(",")
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp|gif|bmp|avif));base64", header)
    if not match:
        raise HTTPException(404, "Image not found")
    try:
        data = base64.b64decode(payload, validate=True)
    except binascii.Error:
        raise HTTPException(404, "Image not found") from None
    headers = {"Cache-Control": "private, max-age=60"}
    if preview:
        thumbnail = await to_thread.run_sync(render_preview, BytesIO(data), limiter=preview_limiter)
        if thumbnail is not None:
            return Response(thumbnail, media_type="image/webp", headers=headers)
        raise HTTPException(422, "Image preview unavailable")
    return Response(data, media_type=match[1], headers=headers)


@router.get("/gallery/{task_id}/metadata")
async def get_gallery_metadata(task_id: int, session: AsyncSession = Depends(get_async_session)):
    inputs = await session.scalar(select(ExecutionTask.input_json).where(ExecutionTask.id == task_id))
    entries = [
        (key, value) for key, value in (inputs or {}).items()
        if isinstance(value, (str, int, float))
        and not (isinstance(value, str) and value.startswith("data:"))
    ]
    named = [(key, value) for key, value in entries
             if isinstance(value, str) and value and re.search(r"prompt|提示|描述", key, re.I)]
    fallback = [(key, value) for key, value in entries
                if isinstance(value, str) and 20 <= len(value) <= 16_000]
    prompt_key, prompt = max(named or fallback, key=lambda pair: len(pair[1]), default=(None, ""))
    fields = [
        {"label": key, "value": str(value).lower() if isinstance(value, bool) else str(value)[:1000]}
        for key, value in entries if key != prompt_key
    ][:64]
    return {"prompt": prompt[:16_000], "fields": fields}


@router.post("/record")
async def record_task(
    body: dict,
    session: AsyncSession = Depends(get_async_session),
):
    """Record a task from frontend execution."""
    status = body.get("status", "completed")
    if status not in VALID_TASK_STATUSES:
        raise HTTPException(400, f"Invalid status: {status}")

    task = ExecutionTask(
        workflow_name=str(body.get("workflow_name", ""))[:100],
        status=status,
        nodes_total=int(body.get("nodes_total", 0)),
        nodes_done=int(body.get("nodes_done", 0)),
        error=str(body["error"])[:2000] if body.get("error") else None,
        duration_ms=int(body["duration_ms"]) if body.get("duration_ms") is not None else None,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    result = _task_to_dict(task)
    await ws_manager.broadcast_task_update("created", result)
    return result


@router.get("/{task_id}")
async def get_task(
    task_id: int,
    session: AsyncSession = Depends(get_async_session),
):
    task = await session.get(ExecutionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return _task_to_dict(task)


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    """对齐 ComfyUI /interrupt 行为:running 任务真正中止(不只是改 DB)。
    桥接 HTTP cancel → RunnerClient.abort 给所有 runner(image/tts);runner 内置 cancel_flag 通路
    + 真 adapter 的 callback_on_step_end check → step 边界 raise CancelledError → 落 cancelled
    NodeResult。修了「点 cancel 但 GPU kernel 还跑完整轮」的真 bug。"""
    task = await session.get(ExecutionTask, task_id)
    if not task:
        raise HTTPException(404)
    if task.status not in ("queued", "running"):
        raise HTTPException(400, "Can only cancel queued or running tasks")
    # 向 runners 发 Abort(广播给所有 group;不知道任务在哪个 runner 上,每个 runner 自己
    # 据 task_id 决定;没有该任务的 runner 收到也只是设个 flag 然后被 pop,无副作用)。
    runner_clients = getattr(request.app.state, "runner_clients", None) or {}
    for group_id, client in runner_clients.items():
        try:
            await client.abort(task_id)
        except Exception as e:  # noqa: BLE001 —— 单 runner 失败不阻断其它 / 不阻断 DB 落态
            logger.warning("cancel_task: runner %s abort(%d) failed: %s", group_id, task_id, e)
    task.status = "cancelled"
    await session.commit()
    await ws_manager.broadcast_task_update("updated", _task_to_dict(task))
    return {"status": "cancelled"}


@router.post("/{task_id}/retry")
async def retry_task(
    task_id: int,
    session: AsyncSession = Depends(get_async_session),
):
    task = await session.get(ExecutionTask, task_id)
    if not task or task.status not in ("failed", "cancelled"):
        raise HTTPException(400, "Can only retry failed or cancelled tasks")
    # Create a new task with same workflow
    new_task = ExecutionTask(
        workflow_id=task.workflow_id,
        workflow_name=task.workflow_name,
        status="queued",
        nodes_total=task.nodes_total,
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)
    return _task_to_dict(new_task)


@router.delete("/{task_id}")
async def delete_task(
    task_id: int,
    session: AsyncSession = Depends(get_async_session),
):
    task = await session.get(ExecutionTask, task_id)
    if not task:
        raise HTTPException(404)
    task_dict = _task_to_dict(task)
    await session.delete(task)
    await session.commit()
    await ws_manager.broadcast_task_update("deleted", task_dict)
    return {"status": "deleted"}



async def collect_referenced_image_uuids(session) -> set[str]:
    """所有 ExecutionTask.result 引用的图片 uuid(文件 stem)集合。reaper 用来保留仍被任务
    历史引用的图(图寿命=任务寿命,spec 2026-06-09 run-history)。每 6h 扫全表 result 可接受;
    任务量极大时再优化(靠任务保留策略兜底磁盘上界)。"""
    from sqlalchemy import select

    stems: set[str] = set()
    rows = await session.execute(select(ExecutionTask.result))
    for (res,) in rows.all():
        for url in _image_urls(res, limit=10_000):
            stem = _uuid_from_image_url(url)
            if stem:
                stems.add(stem)
    return stems


