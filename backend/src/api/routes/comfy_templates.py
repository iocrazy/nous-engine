"""模板即服务(ComfyUI workflow bridge Task 3)—— comfy_templates 表 + 注册/映射 API。

每个上传的 ComfyUI API-format workflow JSON 注册成一个模板,同时建一个
`ServiceInstance`(source_type="comfy_template")作为对外服务入口。服务的
`workflow_snapshot` 不是 ComfyUI JSON 本身,而是固定的桥快照(单
`comfyui_workflow` 节点 + `video_output` 节点,见 spec)——真正的节点类型由
Task 6 提供,这里只是把数据形状定下来。`exposed_inputs` 的映射把 nous 侧
`node_id="bridge"` 与 ComfyUI 侧 `comfy_node_id`/`comfy_input` 并存,前者供
`apply_inputs_to_snapshot` 用,后者供重新上传时的 stale 校验用。
"""

from __future__ import annotations

import asyncio
import os
import time
import urllib.parse
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from src.api.deps_admin import require_admin
from src.api.response_cache import invalidate
from src.models.comfy_template import ComfyTemplate
from src.models.database import get_async_session
from src.models.service_instance import ServiceInstance
from src.services.comfy.client import ComfyError
from src.services.comfy.client import get_comfy_client as get_client
from src.services.workflow_snapshot import NAME_RE, NAME_RULE_MSG

router = APIRouter(prefix="/api/v1/comfy-templates", tags=["comfy-templates"])
health_router = APIRouter(prefix="/api/v1/comfy", tags=["comfy-health"])

# Module-level cache for object_info: (timestamp, data) tuple or None
_object_info_cache: tuple[float, dict] | None = None


def _validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ValueError(NAME_RULE_MSG)
    return name


def _bridge_snapshot(template_id: int) -> dict:
    return {
        "nodes": [
            {"id": "bridge", "type": "comfyui_workflow", "data": {"template_id": template_id}},
            {"id": "out", "type": "video_output", "data": {}},
        ],
        "edges": [
            {"id": "e1", "source": "bridge", "sourceHandle": "outputs",
             "target": "out", "targetHandle": "outputs"},
        ],
    }


# 文件类输入 = 上传文件,不是从清单里选(见 _numeric_constraints / service_schema)。
_FILE_IN_TYPES = {"image", "file", "audio", "video", "binary", "media"}

OutputKind = Literal["image", "video"]
# 靠输出节点的 class_type 判模板产出图片还是视频。只列「落盘的产物节点」:PreviewImage /
# 对比器之类不算(桥的 collect_outputs 在有主图时也会滤掉它们)。都不认识 → 视频,
# 即本修复之前所有模板的默认,老模板行为不变。
_VIDEO_OUTPUT_CLASSES = frozenset({"VHS_VideoCombine", "SaveVideo", "SaveWEBM"})
_IMAGE_OUTPUT_CLASSES = frozenset({"SaveImage", "Image Save", "SaveImageWebsocket"})


def _infer_output_kind(workflow: dict[str, Any]) -> OutputKind:
    classes = {str((n or {}).get("class_type") or "") for n in workflow.values()
               if isinstance(n, dict)}
    if classes & _VIDEO_OUTPUT_CLASSES:
        return "video"
    if classes & _IMAGE_OUTPUT_CLASSES:
        return "image"
    return "video"


def _bridge_exposed_outputs(kind: OutputKind) -> list[dict]:
    """发布契约的输出声明,按模板产物类型指向桥信封里的 `video_url` 或 `image_url`。

    `node_id="out"` 是桥快照的终端 `video_output` 节点(原样透传桥节点信封,见
    comfy_bridge.py 的 `_envelope`);SchemaDrivenOutput.pluck() 按 (node_id, input_name)
    取值,`/v1/services/{name}/schema` 的 output_schema 也由它生成。
    """
    if kind == "image":
        return [{"key": "image_url", "node_id": "out", "input_name": "image_url",
                 "type": "image", "label": "图片"}]
    return [{"key": "video_url", "node_id": "out", "input_name": "video_url",
             "type": "video", "label": "视频"}]


# ---------- Pydantic shapes ----------


class CreateTemplateBody(BaseModel):
    name: str
    workflow: dict[str, Any]
    # 模板产出图片还是视频;不给就按 workflow 的输出节点推断(_infer_output_kind)。
    output_kind: OutputKind | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_name(v)


class ExposedParamMapping(BaseModel):
    key: str
    label: str = ""
    type: str = "string"
    default: Any = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: list | None = None
    required: bool = True
    random: bool = False
    # 多选 combo:值仍是**逗号分隔字符串**,不是数组 —— ComfyUI-Easy-Use 的
    # select_styles 本来就 `.split(',')`(prompt.py:196)。前端据此渲多选。
    multiple: bool = False
    # 选项依赖:本字段的合法值域由**另一个 exposed_param 的当前值**决定(如
    # `styles` 的清单随 `style_pack` 变)。`options_depends_on` 写那个参数的 key,
    # `options_source` 写去哪儿取清单。做成 Literal 而不是自由字符串:以后加别的
    # 来源(lora / checkpoint 清单…)时是加一个枚举值,协议形状不用改。
    options_depends_on: str | None = None
    options_source: Literal["comfy_styles"] | None = None
    # 可选文件参数调用方没传(且无 default)时,桥把它指向的节点连同只为它服务的下游支路
    # 从 graph 里剪掉(comfy/graph_prune.py),而不是把模板里烤死的占位图喂进模型。
    # 默认 False:老模板行为一字不变。
    omit_when_empty: bool = False
    comfy_node_id: str
    comfy_input: str


class MappingBody(BaseModel):
    exposed_params: list[ExposedParamMapping]

    @model_validator(mode="after")
    def _check_options_depends_on(self) -> MappingBody:
        """`options_depends_on` 必须指向**同一份 mapping 里存在的 key**(且不能是自己)。

        指向不存在的 key 会让运行期永远解析不出包名 → 静默退回静态 enum,用户看到的
        是"切了包但没联动"这种查不出原因的症状。发布时就拒掉(全仓的
        RequestValidationError handler 把请求体校验失败统一翻成 **400 +
        code=validation_error**,不是裸 FastAPI 的 422)。
        """
        keys = {p.key for p in self.exposed_params}
        for p in self.exposed_params:
            dep = p.options_depends_on
            # 文件类字段的值是**上传的文件**(桥把 data URI 落到 sidecar),不是从清单里
            # 选一项。给它挂动态清单 = 运行期 resolve_dynamic_enums 拿一份风格名当白名单,
            # 上传必 422 —— 正是 2026-08-12 那个「静态 enum 把上传判非法」的回归换条路
            # 复现。发布时就拒,别等实机。
            if str(p.type or "").lower() in _FILE_IN_TYPES and (
                dep is not None or p.options_source is not None
            ):
                raise ValueError(
                    f"exposed_param {p.key!r}: 文件类参数(type={p.type!r})不能声明 "
                    "options_depends_on/options_source —— 它的值是上传的文件,不是从"
                    "选项清单里选一项,挂上动态清单会让上传被白名单拒掉")
            if p.omit_when_empty:
                if str(p.type or "").lower() not in _FILE_IN_TYPES:
                    raise ValueError(
                        f"exposed_param {p.key!r}: omit_when_empty 只用于文件类参数"
                        f"(当前 type={p.type!r})")
                if p.required:
                    raise ValueError(
                        f"exposed_param {p.key!r}: omit_when_empty 与 required=true 矛盾"
                        "(必填参数永远不会「未传」)")
            if dep is None:
                continue
            if dep == p.key:
                raise ValueError(f"exposed_param {p.key!r}: options_depends_on 不能指向自己")
            if dep not in keys:
                raise ValueError(
                    f"exposed_param {p.key!r}: options_depends_on={dep!r} "
                    f"不是本 mapping 里的 key(现有:{sorted(keys)})")
            if p.options_source is None:
                raise ValueError(
                    f"exposed_param {p.key!r}: 声明了 options_depends_on 就必须同时给 "
                    "options_source(否则运行期不知道去哪儿取清单)")
        return self


class ReuploadBody(BaseModel):
    workflow: dict[str, Any]


# ---------- Helpers ----------


async def _get_template_and_service(
    session: AsyncSession, template_id: int,
) -> tuple[ComfyTemplate, ServiceInstance]:
    tpl = await session.get(ComfyTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, detail="template not found")
    stmt = (
        select(ServiceInstance)
        .options(undefer(ServiceInstance.exposed_inputs))
        .where(ServiceInstance.source_type == "comfy_template")
        .where(ServiceInstance.source_id == template_id)
    )
    svc = (await session.execute(stmt)).scalar_one_or_none()
    if svc is None:
        raise HTTPException(404, detail="service not found for template")
    return tpl, svc


def _mapping_to_exposed_input(m: ExposedParamMapping) -> dict:
    """展开成 exposed_inputs 存储项:node_id/input_name 固定指向桥节点(供
    apply_inputs_to_snapshot 用),comfy_node_id/comfy_input 保留原 ComfyUI
    定位(供重新上传时的 stale 校验用)。"""
    return {
        "key": m.key,
        "label": m.label,
        "node_id": "bridge",
        "input_name": m.key,
        "type": m.type,
        "default": m.default,
        # I3 fix:min/max/step/options/random 嵌套进 `constraints`,不再是顶层平铺键——
        # 这是前端 SchemaDrivenForm.classifyField / isRandomizable(见 SchemaDrivenForm.tsx:
        # 34-61)以及 service_schema.py::_input_property 实际读取的形状(都读
        # `param.constraints.{min,max,step,enum,random}`,不读顶层)。旧的平铺写法两边都
        # 读不到——Playground 数字字段永远渲不成 slider,随机种子按钮也出不来。
        "constraints": _numeric_constraints(m),
        "required": m.required,
        # 桥节点直接读顶层(与 required 同层),不进 constraints:它不是取值约束,
        # 是「没传时怎么改图」的渲染行为。
        "omit_when_empty": m.omit_when_empty,
        "comfy_node_id": m.comfy_node_id,
        "comfy_input": m.comfy_input,
    }


def _split_options(options: list) -> tuple[list, list[dict] | None]:
    """combo 选项 → (纯值 enum, 元数据列表或 None)。

    支持两种写法混排:裸标量(`"sai-anime"`)和富对象(`{value, label?, image?}`)。
    **enum 永远是纯值列表** —— JSON Schema 校验、`/v1/apps` 的入参白名单、以及旧前端
    的 `<select>` 渲染都依赖这一点,元数据绝不能混进去。

    没有任何一项带 label/image 时返回 `None`,`option_meta` 就不写 —— 裸标量的老行为
    一字不变(零回归)。
    """
    values: list = []
    meta: list[dict] = []
    rich = False
    for o in options:
        if isinstance(o, dict) and "value" in o:
            values.append(o["value"])
            m: dict = {"value": o["value"]}
            if o.get("label") is not None:
                m["label"] = o["label"]
            if o.get("image") is not None:
                m["image"] = o["image"]
            if len(m) > 1:
                rich = True
            meta.append(m)
        else:
            values.append(o)
            meta.append({"value": o})
    return values, (meta if rich else None)


def _numeric_constraints(m: ExposedParamMapping) -> dict[str, Any]:
    c: dict[str, Any] = {}
    if m.min is not None:
        c["min"] = m.min
    if m.max is not None:
        c["max"] = m.max
    if m.step is not None:
        c["step"] = m.step
    # 文件类字段不存 enum:ComfyUI 给的 options 是 sidecar 已有文件清单(LoadImage.image
    # → input/ 目录),不是取值域。存下来会让 Playground 渲成下拉框、后端把上传新文件
    # 判成非法(2026-08-12 实机 400)。治本在这:根本不写进去。
    is_file = str(m.type or "").lower() in _FILE_IN_TYPES
    if m.options and not is_file:
        values, option_meta = _split_options(m.options)
        c["enum"] = values
        # 每选项的显示名 + 缩略图(如 ComfyUI-Easy-Use 的 /easyuse/prompt/styles 返回的
        # name_cn / thumbnail)。只在真有元数据时才写,裸标量选项不产生这个键。
        if option_meta is not None:
            c["option_meta"] = option_meta
    # `multiple` 描述的是**值的形状**(逗号分隔串,允许选多项),跟"有没有冻结一份静态
    # enum"是两件事。曾经把它嵌在 `if m.options` 里 —— 只声明依赖、不冻结静态 options 的
    # mapping 于是丢了这个标志,schema 没有 x-multiple,运行期 validate_service_input
    # 拿动态清单整串比对 'a,b' → 每次多选必 422(2026-08-31 那个 bug 从新路径回来)。
    if m.multiple:
        c["multiple"] = True
    if m.random:
        c["random"] = m.random
    # 选项依赖与静态 enum 正交:即便这次没冻结静态 options,依赖关系也照存。
    # service_schema 据此输出 x-options-depends-on / x-options-source,前端据此改成
    # 运行期按依赖参数拉清单。文件类字段一律不存 —— MappingBody 已经在入口拒了,这里
    # 是第二道保险(老数据/直接调用 helper 的路径)。
    if not is_file:
        if m.options_depends_on:
            c["options_depends_on"] = m.options_depends_on
        if m.options_source:
            c["options_source"] = m.options_source
    return c


def _exposed_input_to_param(item: dict) -> dict:
    """exposed_inputs 存储项 → 编辑器吃的平铺 `ComfyExposedParam` 形(min/max/step/
    options/random 从 `constraints` 里读出来摊平)。兼容 I3 修复前写入的旧行——那些行
    没有 `constraints` 键,直接退回顶层同名字段(迁移期防止已保存映射的 min/max 消失)。
    """
    c = item.get("constraints")
    if not isinstance(c, dict):
        c = {}
    return {
        "key": item.get("key"),
        "label": item.get("label", ""),
        "type": item.get("type", "string"),
        "default": item.get("default"),
        "min": c.get("min", item.get("min")),
        "max": c.get("max", item.get("max")),
        "step": c.get("step", item.get("step")),
        # 有 option_meta 就还原富形态(编辑器 round-trip 要拿回 label/image);
        # 否则退回纯 enum,再退回 I3 修复前的顶层 options。
        "options": c.get("option_meta") or c.get("enum", item.get("options")),
        "required": item.get("required", True),
        "random": c.get("random", item.get("random", False)),
        "multiple": c.get("multiple", item.get("multiple", False)),
        "options_depends_on": c.get("options_depends_on", item.get("options_depends_on")),
        "options_source": c.get("options_source", item.get("options_source")),
        "omit_when_empty": bool(item.get("omit_when_empty", False)),
        "comfy_node_id": item.get("comfy_node_id"),
        "comfy_input": item.get("comfy_input"),
    }


def _stale_keys(exposed_inputs: list[dict], workflow: dict) -> list[str]:
    stale = []
    for item in exposed_inputs:
        node_id = item.get("comfy_node_id")
        input_name = item.get("comfy_input")
        node = workflow.get(node_id)
        if node is None or input_name not in (node.get("inputs") or {}):
            stale.append(item.get("key"))
    return stale


# ---------- Routes ----------


@router.post("", status_code=201, dependencies=[Depends(require_admin)])
async def create_template(
    body: CreateTemplateBody,
    session: AsyncSession = Depends(get_async_session),
):
    existing = await session.scalar(
        select(ComfyTemplate).where(ComfyTemplate.name == body.name)
    )
    if existing is not None:
        raise HTTPException(409, detail=f"template name '{body.name}' already exists")
    existing_svc = await session.scalar(
        select(ServiceInstance).where(ServiceInstance.name == body.name)
    )
    if existing_svc is not None:
        raise HTTPException(409, detail=f"service name '{body.name}' already exists")

    output_kind = body.output_kind or _infer_output_kind(body.workflow)
    tpl = ComfyTemplate(name=body.name, workflow_json=body.workflow)
    session.add(tpl)
    await session.flush()

    svc = ServiceInstance(
        name=body.name,
        type="workflow",
        status="active",
        source_type="comfy_template",
        source_id=tpl.id,
        category="app",
        meter_dim="calls",
        workflow_snapshot=_bridge_snapshot(tpl.id),
        exposed_inputs=[],
        # I1 fix:不种 exposed_outputs,`/v1/services/{name}/schema` 的 output_schema
        # 就是空 {}——SchemaDrivenOutput 在 Playground 里没有任何 declared output 可渲染,
        # 只能整坨 dump 原始 JSON。桥快照(_bridge_snapshot)固定的终端节点是 id="out"
        # 的 video_output,它把桥节点信封原样透传(见 comfy_bridge.py `_envelope`)。
        # 图片模板曾经也被写死成 video_url(永远是 null)—— 现在按模板产物类型声明。
        exposed_outputs=_bridge_exposed_outputs(output_kind),
    )
    session.add(svc)
    # round4-style TOCTOU guard (see services.py quick_provision): the precheck
    # above is not atomic with the insert — a concurrent same-name create can
    # still race past it and hit the UNIQUE constraint at commit time. Convert
    # that into a 409 instead of letting IntegrityError bubble to a 500.
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, detail=f"template/service name '{body.name}' already exists")

    invalidate("services")
    return {
        "id": str(tpl.id),
        "name": tpl.name,
        "service_name": svc.name,
        "node_count": len(body.workflow),
        "output_kind": output_kind,
    }


@router.get("", dependencies=[Depends(require_admin)])
async def list_templates(session: AsyncSession = Depends(get_async_session)):
    tpls = (await session.execute(select(ComfyTemplate))).scalars().all()
    out = []
    for tpl in tpls:
        stmt = (
            select(ServiceInstance)
            .options(undefer(ServiceInstance.exposed_inputs))
            .where(ServiceInstance.source_type == "comfy_template")
            .where(ServiceInstance.source_id == tpl.id)
        )
        svc = (await session.execute(stmt)).scalar_one_or_none()
        out.append({
            "id": str(tpl.id),
            "name": tpl.name,
            "service_name": svc.name if svc else None,
            "node_count": len(tpl.workflow_json or {}),
            "exposed_count": len(svc.exposed_inputs or []) if svc else 0,
        })
    return out


@router.get("/{template_id}", dependencies=[Depends(require_admin)])
async def get_template(
    template_id: int,
    session: AsyncSession = Depends(get_async_session),
):
    tpl, svc = await _get_template_and_service(session, template_id)
    return {
        "id": str(tpl.id),
        "name": tpl.name,
        "service_name": svc.name,
        "workflow_json": tpl.workflow_json,
        "exposed_params": [_exposed_input_to_param(i) for i in (svc.exposed_inputs or [])],
    }


@router.put("/{template_id}/mapping", dependencies=[Depends(require_admin)])
async def update_mapping(
    template_id: int,
    body: MappingBody,
    session: AsyncSession = Depends(get_async_session),
):
    _tpl, svc = await _get_template_and_service(session, template_id)
    svc.exposed_inputs = [_mapping_to_exposed_input(m) for m in body.exposed_params]
    await session.commit()
    invalidate("services")
    return {"ok": True}


@router.put("/{template_id}", dependencies=[Depends(require_admin)])
async def reupload_template(
    template_id: int,
    body: ReuploadBody,
    session: AsyncSession = Depends(get_async_session),
):
    tpl, svc = await _get_template_and_service(session, template_id)
    tpl.workflow_json = body.workflow
    stale = _stale_keys(svc.exposed_inputs or [], body.workflow)
    await session.commit()
    invalidate("services")
    return {"stale_keys": stale}


@router.delete("/{template_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_template(
    template_id: int,
    session: AsyncSession = Depends(get_async_session),
):
    # 不走 _get_template_and_service:那个 helper 在配对服务缺失时抛 404,会让
    # **孤儿模板永远删不掉**(历史上先删了服务的行就是这样卡在库里的,2026-08-31
    # 实机 16 行)。这里只要模板在就删,服务有就一起删、没有就跳过。
    tpl = await session.get(ComfyTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, detail="template not found")
    svc = (await session.execute(
        select(ServiceInstance)
        .where(ServiceInstance.source_type == "comfy_template")
        .where(ServiceInstance.source_id == template_id)
    )).scalar_one_or_none()
    if svc is not None:
        await session.delete(svc)
    await session.delete(tpl)
    await session.commit()
    invalidate("services")


# ---------- ComfyUI Health & Object Info Proxy Routes ----------


def _devices_from_stats(stats: dict) -> list[dict]:
    """system_stats() 的 devices 数组 → 前端要的精简形状(原始字节,前端自己格式化)。

    **两个占用是两回事,别混**(2026-08-12 用户实机抓到的误导):
      `vram_total - vram_free` = **整卡**已用 —— `torch.cuda.mem_get_info()` 的全局读数,
        把同卡上别的进程(nous 自己的 vLLM、桌面进程)统统算进去;
      `torch_vram_total`       = **ComfyUI 自占** —— 它 torch 分配器的
        `reserved_bytes.all.current`,这才是"释放显存"能动的那部分。
    实测:整卡已用 43.8G 而 ComfyUI 自占仅 0.1G(那 39.3G 是 qwen3_6_35b_a3b_fp8)。
    """
    out = []
    for d in stats.get("devices") or []:
        vram_total = d.get("vram_total", 0) or 0
        vram_free = d.get("vram_free", 0) or 0
        out.append({
            "name": d.get("name", ""),
            "type": d.get("type", ""),
            "index": d.get("index"),
            "vram_total": vram_total,
            "vram_free": vram_free,
            "vram_used": vram_total - vram_free,          # 整卡已用(含别的进程)
            "comfy_used": d.get("torch_vram_total", 0) or 0,  # ComfyUI 自占
            "torch_vram_total": d.get("torch_vram_total", 0) or 0,
        })
    return out


@health_router.get("/health")
async def comfy_health():
    """Proxy ComfyUI health status + resolved configuration + per-device VRAM."""
    client = get_client()
    health_result = await client.health()
    # Merge with configuration info
    base_url = os.getenv("NOUS_COMFY_URL", "http://127.0.0.1:8188")
    timeout_s = int(os.getenv("NOUS_COMFY_TIMEOUT", "14400"))
    health_result["base_url"] = base_url
    health_result["timeout_s"] = timeout_s
    health_result["devices"] = (
        _devices_from_stats(await client.system_stats()) if health_result.get("online") else []
    )
    # 谁占着渲染信号量、占了多久(空闲时 None)。桥节点一次只放一个渲染进 sidecar,
    # 卡住时所有 comfy 服务一起堵 —— 把这两个数摆在健康面板上,排障不用翻日志猜
    # (2026-09-03 事故:某个 wait 干等了 35 分钟才被发现)。延迟 import 是为了不让
    # 路由模块在 import 期就拉进节点层(predictions.py 里同样的写法)。
    from src.services.nodes import comfy_bridge  # noqa: PLC0415
    health_result["running_render"] = comfy_bridge.get_running_render()
    return health_result


def _used_of(devices: list[dict]) -> int:
    """释放判定只看 **ComfyUI 自占**(torch reserved)。用整卡已用会被同卡的 vLLM
    加载/卸载干扰,把别人的波动误判成"释放成功/失败"。"""
    return sum(d.get("comfy_used", 0) or 0 for d in devices)


# 释放后的轮询节奏(测试 monkeypatch 成 0 免得空等 6s)。
_FREE_POLL_ROUNDS = 12
_FREE_POLL_INTERVAL = 0.5
# ComfyUI 自占低于此值视为"没模型可卸"(空载时 torch reserved 只剩几百 MB 上下文)。
_FREE_NOOP_THRESHOLD = 1_000_000_000


@health_router.post("/free", dependencies=[Depends(require_admin)])
async def comfy_free():
    """释放 ComfyUI 侧缓存模型占用的显存(POST /free,unload_models+free_memory)。

    ComfyUI 的 /free **是异步的**:server.py 只 `prompt_queue.set_flag(...)`,真正的
    `unload_all_models()` 要等 main.py 的 worker 被 `not_empty.notify()` 唤醒后才跑
    (实测 64.3G → 43.8G 用了几秒)。所以调完立刻拍 system_stats 必然还是旧值,按钮
    看着像没生效。这里轮询到显存真降下来(或 6s 超时)再返回快照,`settled` 告诉前端
    这份数字是否已经落定。
    """
    client = get_client()
    try:
        before = _used_of(_devices_from_stats(await client.system_stats()))
        await client.free()
    except ComfyError as e:
        raise HTTPException(e.status_code, detail=e.message)
    except httpx.HTTPError as e:
        raise HTTPException(502, detail=f"ComfyUI sidecar 不可达:{str(e)[:100]}")

    # 本来就没占多少(只剩 torch 上下文)→ 无可释放,直接落定,别让 UI 说"仍在卸载"。
    if before <= _FREE_NOOP_THRESHOLD:
        return {"ok": True, "settled": True, "freed_bytes": 0,
                "devices": _devices_from_stats(await client.system_stats())}

    devices: list[dict] = []
    settled = False
    for _ in range(_FREE_POLL_ROUNDS):  # 默认 12 × 0.5s = 6s 上限
        await asyncio.sleep(_FREE_POLL_INTERVAL)
        devices = _devices_from_stats(await client.system_stats())
        # 归还 >1GB 才算落定 —— 显存读数本身有百 MB 级抖动(同卡其它进程)。
        if before - _used_of(devices) > 1_000_000_000:
            settled = True
            break
    return {"ok": True, "settled": settled, "freed_bytes": max(0, before - _used_of(devices)),
            "devices": devices}


@health_router.get("/object-info")
async def comfy_object_info():
    """Proxy ComfyUI object_info with 60s TTL cache."""
    global _object_info_cache
    now = time.monotonic()

    # Check cache validity (60s TTL)
    if _object_info_cache is not None:
        cached_time, cached_data = _object_info_cache
        if now - cached_time < 60:
            return cached_data

    # Cache miss or expired: fetch from client
    client = get_client()
    try:
        data = await client.object_info()
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(
            502,
            detail=f"ComfyUI sidecar 不可达或返回无效响应:{str(e)[:100]}"
        )
    _object_info_cache = (now, data)
    return data


def _has_dotdot(path: str) -> bool:
    """路径里有没有 `..` 段(`/` 和 `\\` 都算分隔符)。"""
    return any(seg == ".." for seg in path.replace("\\", "/").split("/"))


def _thumbnail_url(thumbnail: str) -> str | None:
    """sidecar 的 thumbnail → 浏览器真能加载的地址;转不了 → None(该项退回文字卡片)。

    fooocus_styles 给的是 `https://raw.githubusercontent.com/...` 绝对外链,原样透传;
    krea2 那批包给的是 `/easyuse/prompt/styles/image?path=./samples/x.jpg` 这种
    **sidecar 侧相对路径** —— 浏览器会拿 nous 自己的 origin(:8000)去解析,必 404。
    从中把 `path=` 的值抠出来,改写成走下面的代理端点。

    **只传 path、不传整条 URL**:代理端点若接受任意 `src` 再转发,httpx 归一化点段会让
    `/easyuse/../history` 变成 `/history`,前缀白名单形同虚设(见 ComfyClient.style_image)。
    """
    if thumbnail.startswith(("http://", "https://", "data:")):
        return thumbnail
    q = urllib.parse.urlsplit(thumbnail).query
    path = (urllib.parse.parse_qs(q).get("path") or [None])[0]
    # 没有 path= 的相对地址代理不了(路由是写死的,只认这一个参数)—— 宁可不给 image,
    # 让前端退回纯文字卡片,也不吐一个必然 404 的地址。
    if not path or _has_dotdot(path):
        return None
    return "/api/v1/comfy/style-image?path=" + urllib.parse.quote(path, safe="")


def _style_to_option(item: dict) -> dict:
    """sidecar 风格项 → mapping 直接能吃的 `{value, label?, image?}`。

    只在源数据真有 name_cn / thumbnail 时才写对应键 —— 不编造 label(否则前端分不清
    "有中文名"和"回退用了英文名"),也不编造 image(缩略图缺失时该退回纯文字卡片)。
    """
    opt: dict = {"value": item.get("name")}
    if item.get("name_cn"):
        opt["label"] = item["name_cn"]
    if item.get("thumbnail"):
        image = _thumbnail_url(item["thumbnail"])
        if image:
            opt["image"] = image
    return opt


@health_router.get("/style-image", dependencies=[Depends(require_admin)])
async def comfy_style_image(
    path: str = Query(..., description="缩略图文件路径,取自 /styles 返回的 image 里的 path="),
):
    """风格缩略图代理 —— **只**代理 ComfyUI-Easy-Use 的缩略图这一条路由。

    收的是文件路径而不是 URL:早先那版收 `src` 再转发、靠 `startswith("/easyuse/")`
    把关,而 httpx 合并相对 URL 时会归一化点段 —— `/easyuse/../history` 到了 sidecar
    就是 `/history`,于是这个端点等于"拿 admin 身份任意打 sidecar GET"。写死路由 +
    `params={"path": …}` 从根上没有这个可能;`..` 段再挡一道(sidecar 侧自己去解析
    文件路径,别让它接到能跳出 styles 目录的东西)。

    缓存用 **private**:这个端点要 admin 鉴权,`public` 会让 cloudflared / 中间缓存
    把字节回给没鉴权的人。
    """
    if _has_dotdot(path):
        raise HTTPException(400, detail="path 不能包含 `..` 段")
    try:
        content, mime = await get_client().style_image(path)
    except (ComfyError, httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, detail=f"ComfyUI sidecar 不可达:{str(e)[:120]}")
    return Response(content=content, media_type=mime,
                    headers={"Cache-Control": "private, max-age=3600"})


@health_router.get("/style-packs", dependencies=[Depends(require_admin)])
async def comfy_style_packs():
    """可选的风格包清单(easy stylesSelector 的 styles combo)。"""
    try:
        packs = await get_client().style_packs()
    except (ComfyError, httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, detail=f"ComfyUI sidecar 不可达:{str(e)[:120]}")
    return {"packs": packs}


@health_router.get("/styles", dependencies=[Depends(require_admin)])
async def comfy_styles(pack: str = Query(..., description="风格包名,取自 /style-packs")):
    """某个风格包内的全部风格,已归一成 exposed_params.options 的 `{value,label,image}` 形。

    前端把返回的 `options` 原样塞进 `PUT /api/v1/comfy-templates/{id}/mapping` 的
    exposed_params 即可 —— 无需再做形状转换。
    """
    try:
        items = await get_client().styles(pack)
    except (ComfyError, httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, detail=f"ComfyUI sidecar 不可达:{str(e)[:120]}")
    return {"pack": pack, "options": [_style_to_option(i) for i in items if i.get("name")]}
