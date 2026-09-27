"""ComfyUI workflow bridge nodes (Task 6).

`comfyui_workflow` is the single inline node every 模板即服务 bridge snapshot
runs through (see `src.api.routes.comfy_templates._bridge_snapshot`): it loads
the raw ComfyUI API-format graph + the service's `exposed_inputs` mapping,
patches user-supplied values into the graph (uploading `media`-typed data
URIs, rolling `random`-marked seeds), submits + waits on the ComfyUI sidecar
serialized through a module-level semaphore (sidecar 显存独占 — one render at
a time), then persists every collected output via `write_media` and returns a
stable envelope. `video_output` is the terminal sink node mirroring
`image.py`'s `image_output` (原样透传).
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import tempfile
import time
from copy import deepcopy
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import undefer

from src.models.comfy_template import ComfyTemplate
from src.models.database import get_session_factory
from src.models.execution_task import ExecutionTask
from src.models.service_instance import ServiceInstance
from src.services.comfy.client import ComfyError
from src.services.comfy.client import get_comfy_client as get_client
from src.services.comfy.outputs import collect_outputs, history_error
from src.services.comfy.thumbnail import extract_first_frame
from src.services.comfy.upload_inputs import check_plain_filename, decode_data_uri
from src.services.image_output_storage import write_image as _write_media_sync
from src.services.nodes.registry import register

logger = logging.getLogger(__name__)

# Sidecar 显存独占:一次只能有一个渲染在跑(submit+wait 串行化,download/storage 不占)。
# 这条串行化假设**单进程部署**——uvicorn 跑无 `--workers`(生产就是这样,见
# infra/systemd/nous-engine-backend.service)。多 worker/多进程部署下每个进程各有
# 自己的 `_SEM`,不再互斥,需要改成跨进程锁(Postgres advisory lock / Redis 等)才能保住
# "sidecar 一次只服务一个渲染"这条不变式。
_SEM = asyncio.Semaphore(1)

# 当前持有 `_SEM`、正在渲染的 ExecutionTask id(None = 空闲)。cancel_prediction
# (predictions.py)据此判断:被取消的 prediction 是不是"真的卡在 ComfyUI 渲染上"——
# 只有是,才转发 `/interrupt`;排在它后面等信号量的任务不该被这个 interrupt 误伤
# (那是别的渲染,C1 fix)。用访问器读,不直接碰 module global(见 get_running_task_id)。
_running_task_id: str | int | None = None
# 上面那个 id 是什么时候拿到信号量的(time.monotonic()),空闲时 None。排障用:
# `/api/v1/comfy/health` 把 (task_id, 已持有秒数) 一起吐出来 —— "所有 comfy 服务
# 都不动了"时一眼看出是谁占着、占了多久(2026-09-03 事故里这两个数得翻日志猜)。
_running_since: float | None = None


def get_running_task_id() -> str | int | None:
    """当前占用渲染信号量的 ExecutionTask id,空闲时 None。"""
    return _running_task_id


def get_running_render() -> dict | None:
    """当前渲染的 `{task_id, held_seconds}`,空闲时 None(健康面板/排障用)。"""
    if _running_task_id is None:
        return None
    held = 0.0 if _running_since is None else max(0.0, time.monotonic() - _running_since)
    return {"task_id": _running_task_id, "held_seconds": round(held, 1)}


async def _task_is_cancelled(task_id: str | int) -> bool:
    """渲染前的 cancel-race 复查:任务在排队等信号量期间可能已被用户取消
    ——拿到信号量后、真正 submit 给 ComfyUI 前再查一次 DB 状态,避免"取消了
    的任务照样悄悄跑完"(C1 fix)。"""
    session_factory = get_session_factory()
    async with session_factory() as session:
        task = await session.get(ExecutionTask, int(task_id))
        return task is not None and task.status == "cancelled"


async def write_media(data: bytes, *, ext: str, ttl_seconds: int = 86400) -> dict:
    """Async wrapper around the sync `image_output_storage.write_image` — disk
    I/O runs off the event loop via a thread so the render-serial semaphore
    above isn't held while we write files."""
    return await asyncio.to_thread(_write_media_sync, data, ext=ext, ttl_seconds=ttl_seconds)


async def load_template(template_id) -> tuple[dict, list[dict]]:
    """Load `(workflow_json, exposed_inputs)` for a comfy_templates row.

    exposed_inputs live on the paired ServiceInstance (source_type=
    "comfy_template", source_id=template_id) — same linkage the route layer
    uses in `comfy_templates._get_template_and_service`.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        tpl = await session.get(ComfyTemplate, int(template_id))
        if tpl is None:
            raise ValueError(f"模板不存在:{template_id}")
        stmt = (
            select(ServiceInstance)
            .options(undefer(ServiceInstance.exposed_inputs))
            .where(ServiceInstance.source_type == "comfy_template")
            .where(ServiceInstance.source_id == tpl.id)
        )
        svc = (await session.execute(stmt)).scalar_one_or_none()
        exposed = list(svc.exposed_inputs or []) if svc is not None else []
        return dict(tpl.workflow_json or {}), exposed


# C2/I1 fix:mapping type 词汇统一——ComfyTemplateEditor.tsx 的 TYPE_OPTIONS 现在能选
# "image"(SchemaDrivenForm.classifyField 认的 file|image|audio|video|binary 集合之一,
# 让 Playground 渲染文件选择器),旧代码这里只认字面量 "media" 一种,导致编辑器选出来的
# 类型永远走不到上传分支(data URI 原样当字符串塞进图,ComfyUI 侧炸)。任意一个"这是待
# 上传素材"型都触发同一条上传逻辑。
_UPLOAD_TYPES = frozenset({"media", "image", "file", "audio", "video"})


def _prevalidate_files(exposed_params: list[dict], data: dict) -> dict[str, tuple[bytes, str, str]]:
    """校验全部文件类入参(不上传)。返回 `{key: (字节, 扩展名, mime)}`,只含 data URI 项。

    调用方给的非 data URI 值只放行 sidecar 上已有的裸文件名,URL / 路径 / `[output]`
    注解一律拒(见 comfy/upload_inputs.py);mapping 的 default 是管理员写的,不在此列。
    """
    decoded: dict[str, tuple[bytes, str, str]] = {}
    for m in exposed_params:
        key = m["key"]
        value = data.get(key, m.get("default"))
        if m.get("type") not in _UPLOAD_TYPES or not isinstance(value, str):
            continue
        if value.startswith("data:"):
            decoded[key] = decode_data_uri(key, m.get("type"), value)
        elif key in data:
            check_plain_filename(key, value)
    return decoded


@register("comfyui_workflow")
class ComfyUIWorkflowNode:
    """Bridge node: patch exposed params into the ComfyUI graph, run it on
    the sidecar, persist outputs. See module docstring for the full flow."""

    async def invoke(self, data: dict, inputs: dict) -> dict:
        template_id = data.get("template_id")
        # workflow_executor._execute_inline_node injects this (same seam as
        # `_node_id`) — the ExecutionTask id this invocation is running under,
        # None for the DB-less unit tests that call invoke() directly.
        task_id = data.get("_task_id")
        graph_template, exposed_params = await load_template(template_id)
        graph = deepcopy(graph_template)
        client = get_client()
        seed = None

        # 先把**所有**文件类入参校验/解码完,再开始上传 —— 否则第二个参数不合法时,
        # 第一个已经传到 sidecar input 目录的文件就成了孤儿。
        decoded = _prevalidate_files(exposed_params, data)

        for m in exposed_params:
            key = m["key"]
            value = data.get(key, m.get("default"))
            node_id = str(m.get("comfy_node_id"))
            input_name = m.get("comfy_input")

            if key in decoded:
                raw, ext, mime = decoded[key]
                filename = f"{secrets.token_hex(8)}.{ext}"
                value = await client.upload_image(filename, raw, mime)

            if m.get("random") and (value is None or value == ""):
                value = secrets.randbelow(2 ** 32)
                seed = value

            if m.get("required") and (value is None or value == ""):
                raise ValueError(f"缺少必填参数:{key}")

            # Intentional: an optional param the caller didn't supply (and
            # with no mapping default) stays untouched rather than patching
            # `None` into the graph — the template's own placeholder value
            # (e.g. a LoadImage node's baked-in default filename) is what
            # ComfyUI should see; writing `None` would corrupt that node's
            # input type instead of "no override".
            if value is None:
                continue

            node = graph.get(node_id)
            if node is not None:
                node.setdefault("inputs", {})[input_name] = value
            else:
                # 映射指向的节点已不在 graph 里(重新上传工作流后没同步映射 ——
                # reupload_template 的 stale-key 检测本该先标红,但保险起见渲染时
                # 也不该静默丢掉这个参数)。
                logger.warning(
                    "comfy_bridge: template=%s 的映射 key=%r 指向节点 %r,"
                    "但该节点不在当前 graph 中(工作流重新上传后映射未更新?),已跳过该参数",
                    template_id, key, node_id,
                )

        # 渲染期间的取消探测(2026-09-03 事故):`/interrupt` 只在 ComfyUI 的节点边界
        # 生效,卡在某个节点内部(比如等一个连接已 CLOSE_WAIT 的 HF 下载)时救不回来,
        # 于是 wait() 会一直等到 NOUS_COMFY_TIMEOUT(默认 4 小时)、全程占着 `_SEM`。
        # 让 wait 自己按低频(默认每 10s)复查 DB 状态,cancelled 就立刻抛错退出、
        # 释放信号量。抛出的 ComfyError 一路冒到 workflow_runner 的 except 分支,那里
        # 先 `SELECT … FOR UPDATE` 重读状态,已是 cancelled 就不覆盖成 failed
        # (honor-cancelled,见 workflow_runner.py)—— 终态仍是 canceled。
        should_abort = None
        if task_id is not None:
            async def should_abort() -> bool:
                return await _task_is_cancelled(task_id)

        global _running_task_id, _running_since
        async with _SEM:
            _running_task_id = task_id
            _running_since = time.monotonic()
            try:
                # cancel-race 复查(C1):本任务可能在排队等信号量期间被取消
                # ——真正 submit 给 ComfyUI 前再查一次 DB,取消了就不渲染。
                if task_id is not None and await _task_is_cancelled(task_id):
                    raise ComfyError("任务已取消,渲染跳过")
                prompt_id = await client.submit(graph)
                timeout_s = float(os.getenv("NOUS_COMFY_TIMEOUT", "14400"))
                history = await client.wait(
                    prompt_id, timeout_s=timeout_s, should_abort=should_abort)
            finally:
                _running_task_id = None
                _running_since = None

        # history 出现 ≠ 渲染成功:节点抛异常时 status_str=="error",outputs 可能空也可能是
        # 半截产物。把 ComfyUI 的错误原文带出去落 failed,别包装成成功(也别只剩一句含糊的
        # 「未产出任何产物」)。
        failure = history_error(history)
        if failure is not None:
            raise ComfyError(failure)

        outputs = collect_outputs(history, graph)
        items: list[dict] = []
        video_url: str | None = None
        thumbnails: list[str] = []

        for o in outputs:
            raw_bytes = await client.download(
                {"filename": o.filename, "subfolder": o.subfolder, "type": o.file_type})
            ext = o.filename.rsplit(".", 1)[-1].lower() if "." in o.filename else "bin"
            stored = await write_media(raw_bytes, ext=ext)
            items.append({
                "url": stored["url"], "kind": o.kind, "filename": o.filename,
                "node_id": o.node_id, "class_type": o.class_type,
            })

            if o.kind == "video":
                if video_url is None:
                    video_url = stored["url"]
                with tempfile.TemporaryDirectory(prefix="nous-bridge-vid-") as tmp_dir:
                    vid_path = Path(tmp_dir) / f"src.{ext}"
                    # Off the event loop — this can be a full video's worth of
                    # bytes (same rationale as the write_media wrapper above).
                    await asyncio.to_thread(vid_path.write_bytes, raw_bytes)
                    frame = await extract_first_frame(vid_path)
                if frame is not None:
                    thumb = await write_media(frame, ext="png")
                    thumbnails.append(thumb["url"])

        if not items:
            # ComfyUI 返回了 history 但没有一个可识别产物 —— 典型是渲染被中断
            # (interrupt 打断在 collect_outputs 认得的输出节点写产物之前)。之前
            # 这里会直接返回一个"成功"的空信封,task 落 completed 但用户拿不到
            # 任何东西——静默失败。改成显式报错,让 workflow_runner 落 failed
            # (除非 DB 已经是 cancelled,那种情况上面的 except 分支会 honor 取消)。
            raise ComfyError("ComfyUI 未产出任何产物(可能被中断)")

        return _envelope(items, video_url, thumbnails, seed)


def _envelope(items: list[dict], video_url: str | None, thumbnails: list[str], seed) -> dict:
    """桥节点的返回信封 —— 按**实际产物**区分图片/视频,一份实现服务所有模板。

    - 有视频产物 → 视频形:`video_url` = 第一个视频(与历来一致,老视频模板零变化)。
    - 没有视频、有图片 → 图片形:多一个 `image_url` = 第一张主图(`collect_outputs`
      已在有主图时滤掉预览/对比图)。`video_url` 键保留为 None —— 形状向后兼容,
      但绝不拿图片地址冒充视频地址。

    `image_url` **只在图片形里出现**:`execution_task_serialize._detect_image_meta` 按
    这个键是否存在判任务类型,视频结果里带个 `image_url: None` 就会被误标成 image 任务。
    发布契约(`exposed_outputs`)按模板的输出类型指向 `video_url` 或 `image_url`,
    见 `comfy_templates._bridge_exposed_outputs`。
    """
    env: dict = {"items": items, "video_url": video_url, "thumbnails": thumbnails, "seed": seed}
    if video_url is None:
        image_url = next((i["url"] for i in items if i.get("kind") == "image"), None)
        if image_url is not None:
            env["image_url"] = image_url
    return env


@register("video_output")
class VideoOutputNode:
    """Terminal sink — 原样透传上游产物(image_output 的 video 对应版)。"""

    async def invoke(self, data: dict, inputs: dict) -> dict:
        outs = inputs.get("outputs")
        if isinstance(outs, dict) and outs:
            return outs
        # 桥快照的边 sourceHandle="outputs" 不是桥信封里的键 → 执行器走 spread 兜底,把
        # 信封各键平铺进 inputs;信封里有 `image_url` 时执行器还会顺手把它按 targetHandle
        # 落一份,即 inputs["outputs"] = 图片 URL 字符串(workflow_executor._get_inputs 的
        # 图像多路输入逻辑)。那个字符串不是信封,摘掉,返回平铺的信封本身。
        return {k: v for k, v in inputs.items() if k != "outputs"}
