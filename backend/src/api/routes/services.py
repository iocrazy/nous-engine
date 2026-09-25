"""v3 services CRUD + quick-provision path.

The two creation paths in v3 produce the same row in `service_instances`:
this module owns "quick-provision" (auto-generated trivial workflow);
`routes/workflow_publish.py` owns "publish from workflow".

Endpoints:
  GET    /api/v1/services                 — list (admin)
  GET    /api/v1/services/{id}            — detail with snapshot (admin)
  POST   /api/v1/services/quick-provision — quick-provision (admin)
  PATCH  /api/v1/services/{id}            — status lifecycle (admin)
  DELETE /api/v1/services/{id}            — delete (admin)
  GET    /api/v1/services/{id}/autostart-preview — 开机会加载哪些模型(确认框用)
  POST   /api/v1/services/{id}/autostart  — 开/关开机启动 (admin)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_serializer, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from src.api.deps_admin import require_admin
import logging

from src.api.response_cache import cached, invalidate
from src.models.database import get_async_session
from src.models.schemas import ExposedParam
from src.models.service_instance import ServiceInstance
from src.models.workflow import Workflow
from src.services.model_capabilities import capabilities_for_service
from src.services.service_autostart import preload_model_infos
from src.services.service_models import extract_service_models
from src.services.workflow_snapshot import (
    _IMAGE_NODE_TYPES,
    _IMAGE_OUTPUT_FIELDS,
    _METER_DIM_BY_CATEGORY,
    _node_ids,
    _node_types_by_id,
    _snapshot_hash,
    NAME_RE,
)

router = APIRouter(prefix="/api/v1", tags=["services"])

logger = logging.getLogger(__name__)

def _validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ValueError(
            "service name must match ^[a-z][a-z0-9-]{1,62}$ "
            "(start with a-z, then a-z/0-9/-, total 2-63 chars)",
        )
    return name


# ---------- Pydantic shapes ----------


class ServiceModelRef(BaseModel):
    """Static ref to a model/component a service depends on. Live load-state
    is overlaid client-side (see src/services/service_models.py)."""
    kind: str  # 'component' | 'engine'
    role: str | None = None  # diffusion_models|clip|vae|checkpoint|llm|tts
    label: str
    file: str | None = None  # component abs path (matched by file)
    engine_key: str | None = None  # registry engine key (matched by name)


class ServiceOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    name: str
    type: str
    status: str
    source_type: str
    source_id: int | None = None
    source_name: str | None = None
    category: str | None = None
    meter_dim: str | None = None
    workflow_id: int | None = None
    workflow_name: str | None = None  # join from workflows.name；UI 用来显示"来自 Workflow · {name}"
    snapshot_hash: str | None = None
    snapshot_schema_version: int = 1
    version: int = 1
    # 开机启动:开机预加载本服务引用的模型(见 services/service_autostart.py)。
    autostart: bool = False
    # 该服务工作流依赖的模型/组件(静态枚举;加载状态前端实时叠加)。
    models: list[ServiceModelRef] = []
    # model 服务的能力(工具/思考/图片/上下文/提供商,见 services/model_capabilities.py);
    # 只在列表端点填,其它服务为 None。
    capabilities: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("id", "source_id", "workflow_id", when_used="json")
    def _to_str(self, v: int | None) -> str | None:
        return str(v) if v is not None else None


class ServiceDetailOut(ServiceOut):
    workflow_snapshot: dict
    exposed_inputs: list
    exposed_outputs: list


class PreloadModelOut(BaseModel):
    """开机会为该服务加载的一个模型。vram_gb/gpu 来自 registry spec,查不到就为 None。"""
    name: str
    vram_gb: float | None = None
    gpu: int | list[int] | None = None


class AutostartBody(BaseModel):
    enabled: bool


class ServiceAutostartOut(ServiceOut):
    """toggle 的返回:更新后的服务 + 开机会加载的模型清单。"""
    preload_models: list[PreloadModelOut] = []


class AutostartPreviewOut(BaseModel):
    """确认框用:开了开机启动后,开机会加载哪些模型。"""
    service_id: str
    name: str
    autostart: bool
    preload_models: list[PreloadModelOut] = []


class ServicePatch(BaseModel):
    status: Literal["active", "paused", "deprecated", "retired"] | None = None
    # 改名(= 改 model 路由键)。⚠️ 用旧 model 名调用的客户端会失效(404),UI 给提示。
    # 格式同发布(^[a-z][a-z0-9-]{1,62}$),唯一;grant/用量靠 service_id 不受影响。
    name: str | None = None
    # 服务页「应用编辑」tab 改暴露字段(逐 widget 表单配置)就地落库。映射改了
    # 不动 snapshot 本体,故不 bump snapshot_hash/version(见 spec 2026-06-09 R3:
    # 改 active 服务的对外 schema 有契约风险,UI 给提示,单管理员 infra 允许)。
    exposed_inputs: list[ExposedParam] | None = None
    exposed_outputs: list[ExposedParam] | None = None


class QuickProvisionBody(BaseModel):
    name: str
    category: Literal["llm", "tts", "vl"]
    engine: str = Field(..., description="Engine key, e.g. 'qwen3-8b' / 'cosyvoice2'")
    label: str = ""
    params: dict[str, Any] = {}
    """Free-form engine knobs (system_prompt, temperature, voice, …)."""

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_name(v)


# ---------- Helpers ----------




def _trivial_workflow_for(category: str, engine: str, params: dict[str, Any]) -> dict:
    """Build the minimum DAG that backs a quick-provisioned service.

    The shape is intentionally tiny — three nodes (input, engine call,
    output) with a fixed wiring. The executor doesn't need to understand
    it during PR-A; the workflow exists so PR-B's "源 Workflow" link in
    the service detail page has something to point at.
    """
    return {
        "schema": "comfy/api-1",
        "nodes": {
            "in_1": {
                "class_type": "PrimitiveInput",
                "inputs": {"value": params.get("default_input", "")},
                "_meta": {"role": "exposed_input"},
            },
            "engine_1": {
                "class_type": f"{category.upper()}Engine",
                "inputs": {
                    "engine": engine,
                    "prompt": ["in_1", 0],
                    **{k: v for k, v in params.items() if k != "default_input"},
                },
            },
            "out_1": {
                "class_type": "PrimitiveOutput",
                "inputs": {"value": ["engine_1", 0]},
                "_meta": {"role": "exposed_output"},
            },
        },
    }


# ---------- Routes ----------


def _service_model_refs(svc: ServiceInstance) -> list[ServiceModelRef]:
    """服务依赖的模型 ref —— 前端按此 overlay 实时加载态(useServiceModelStatus)。

    - 工作流服务:从 workflow_snapshot 抽各节点引用的模型/组件。
    - model 服务(asr/embedding/llm 等直接绑模型,无 snapshot):补一条指向 backing
      引擎的 engine ref(engine_key=source_name),否则服务窗口的「模型」面板恒空、
      看不出模型是否加载(2026-06-21 用户反馈:服务窗口要能显示里面的模型是否加载)。
    """
    if svc.source_type == "model" and svc.source_name:
        return [
            ServiceModelRef(
                kind="engine",
                role=svc.category,
                label=svc.source_name,
                engine_key=svc.source_name,
            )
        ]
    return [ServiceModelRef(**m) for m in extract_service_models(svc.workflow_snapshot)]


@router.get(
    "/services",
    dependencies=[Depends(require_admin)],
)
@cached("services", ttl=30)
async def list_services(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    category: str | None = None,
    status: str | None = None,
):
    # LEFT JOIN workflows 把 name 一起带回来 — UI 服务卡片显示
    # "来自 Workflow · {name} #{id}"。trivial workflow 名是 "trivial:{svc}"，
    # 用户能立刻看出是快速开通生成的 vs 真实 workflow。
    stmt = (
        select(ServiceInstance, Workflow.name.label("workflow_name"))
        .outerjoin(Workflow, Workflow.id == ServiceInstance.workflow_id)
        # un-defer snapshot so we can enumerate each service's model deps for
        # the list-card「已加载 X/Y」badge. Single-admin infra has a handful of
        # services + the response is @cached(ttl=30), so the cost is absorbed.
        .options(undefer(ServiceInstance.workflow_snapshot))
        .order_by(ServiceInstance.created_at.desc())
    )
    if category:
        stmt = stmt.where(ServiceInstance.category == category)
    if status:
        stmt = stmt.where(ServiceInstance.status == status)
    rows = (await session.execute(stmt)).all()
    configs: dict | None = None
    out = []
    for svc, wf_name in rows:
        item = ServiceOut.model_validate(svc)
        item.workflow_name = wf_name
        item.models = _service_model_refs(svc)
        if svc.source_type == "model" and item.models:
            if configs is None:
                from src.config import load_model_configs  # noqa: PLC0415 — 同 engines.py,局部取
                configs = load_model_configs()
            item.capabilities = capabilities_for_service(svc, configs)
        out.append(item)
    return out


@router.get(
    "/services/{service_id}",
    response_model=ServiceDetailOut,
    dependencies=[Depends(require_admin)],
)
async def get_service(
    service_id: int,
    session: AsyncSession = Depends(get_async_session),
):
    stmt = (
        select(ServiceInstance, Workflow.name.label("workflow_name"))
        .outerjoin(Workflow, Workflow.id == ServiceInstance.workflow_id)
        .options(
            undefer(ServiceInstance.workflow_snapshot),
            undefer(ServiceInstance.exposed_inputs),
            undefer(ServiceInstance.exposed_outputs),
        )
        .where(ServiceInstance.id == service_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise HTTPException(404, detail="service not found")
    svc, wf_name = row
    out = ServiceDetailOut.model_validate(svc)
    out.workflow_name = wf_name
    out.models = _service_model_refs(svc)
    return out


@router.post(
    "/services/quick-provision",
    response_model=ServiceDetailOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
async def quick_provision(
    body: QuickProvisionBody,
    session: AsyncSession = Depends(get_async_session),
):
    # Name uniqueness is also enforced by a UNIQUE constraint; this gives a
    # 409 instead of a 500 on collision.
    existing = await session.scalar(
        select(ServiceInstance).where(ServiceInstance.name == body.name)
    )
    if existing is not None:
        raise HTTPException(409, detail=f"service name '{body.name}' already exists")

    snapshot = _trivial_workflow_for(body.category, body.engine, body.params)

    workflow = Workflow(
        name=f"trivial:{body.name}",
        description=f"auto-generated for service {body.name}",
        nodes=[],
        edges=[],
        is_template=False,
        status="active",
        auto_generated=True,
    )
    session.add(workflow)
    await session.flush()

    svc = ServiceInstance(
        name=body.name,
        type="inference",
        status="active",
        source_type="workflow",
        source_id=workflow.id,
        source_name=body.engine,
        category=body.category,
        meter_dim=_METER_DIM_BY_CATEGORY.get(body.category, "calls"),
        workflow_id=workflow.id,
        workflow_snapshot=snapshot,
        snapshot_hash=_snapshot_hash(snapshot),
        snapshot_schema_version=1,
        version=1,
        exposed_inputs=[
            {
                "key": "input",
                "label": body.label or "Input",
                "node_id": "in_1",
                "input_name": "value",
                "type": "string",
                "required": True,
            }
        ],
        exposed_outputs=[
            {
                "key": "output",
                "label": "Output",
                "node_id": "out_1",
                "input_name": "value",
                "type": "string",
            }
        ],
    )
    session.add(svc)
    await session.flush()

    workflow.generated_for_service_id = svc.id
    # round4 #6:名字唯一性预检(上面 line 224)是 TOCTOU —— 并发同名两个请求都过预检,
    # 第二个 commit 抛 IntegrityError。早先无 try/except → 500。捕获 → 409,与预检口径一致。
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, detail=f"service name '{body.name}' already exists")
    # Force-load deferred columns before serializing — pydantic from_attributes
    # would otherwise trigger sync lazy load inside an async context.
    await session.refresh(
        svc,
        attribute_names=["workflow_snapshot", "exposed_inputs", "exposed_outputs"],
    )
    # Cross-resource: quick-provision creates BOTH a workflow row and a service row.
    invalidate("services", "workflows")
    return svc


class RegisterModelBody(BaseModel):
    name: str
    source_name: str  # engine key(如 qwen3_embedding_8b / cosyvoice2)
    type: str = "inference"

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_name(v)


@router.post(
    "/services/register-model",
    response_model=ServiceOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
async def register_model_service(
    body: RegisterModelBody,
    session: AsyncSession = Depends(get_async_session),
):
    """建 model-backed 服务(source_type=model)—— 取代 legacy POST /api/v1/instances,
    双轨收敛(#3)。ModelsOverlay「发布模型为 API 接入点」流程指向这里。category 由引擎
    type 派生(load_model_configs),前端据此给对的调用端点(embedding→/v1/embeddings 等)。"""
    existing = await session.scalar(
        select(ServiceInstance).where(ServiceInstance.name == body.name)
    )
    if existing is not None:
        raise HTTPException(409, detail=f"service name '{body.name}' already exists")

    from src.config import load_model_configs
    category = (load_model_configs().get(body.source_name) or {}).get("type")

    svc = ServiceInstance(
        name=body.name,
        type=body.type,
        status="active",
        source_type="model",
        source_name=body.source_name,
        category=category,
        meter_dim=_METER_DIM_BY_CATEGORY.get(category, "calls") if category else "calls",
    )
    session.add(svc)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, detail=f"service name '{body.name}' already exists")
    await session.refresh(svc)
    out = ServiceOut.model_validate(svc)
    out.models = _service_model_refs(svc)
    invalidate("services")
    return out


@router.patch(
    "/services/{service_id}",
    response_model=ServiceOut,
    dependencies=[Depends(require_admin)],
)
async def patch_service(
    service_id: int,
    body: ServicePatch,
    session: AsyncSession = Depends(get_async_session),
):
    edits_exposed = body.exposed_inputs is not None or body.exposed_outputs is not None
    # workflow_snapshot / exposed_* are deferred() — touching them in the async
    # path without undefer raises MissingGreenlet (lazy load = sync IO). Only
    # pull them when this PATCH actually edits the exposed schema.
    stmt = select(ServiceInstance).where(ServiceInstance.id == service_id)
    if edits_exposed:
        stmt = stmt.options(
            undefer(ServiceInstance.workflow_snapshot),
            undefer(ServiceInstance.exposed_inputs),
            undefer(ServiceInstance.exposed_outputs),
        )
    svc = await session.scalar(stmt)
    if svc is None:
        raise HTTPException(404, detail="service not found")
    if body.status is not None:
        svc.status = body.status
    if body.name is not None and body.name != svc.name:
        # 校验格式 + 唯一(预检;commit 处再兜 IntegrityError 防并发 TOCTOU)。
        try:
            _validate_name(body.name)
        except ValueError as e:
            raise HTTPException(422, detail=str(e))
        taken = await session.scalar(
            select(ServiceInstance.id).where(
                ServiceInstance.name == body.name, ServiceInstance.id != service_id),
        )
        if taken is not None:
            raise HTTPException(409, detail=f"service name '{body.name}' already exists")
        svc.name = body.name
    if edits_exposed:
        _validate_exposed_against_snapshot(
            dict(svc.workflow_snapshot or {}),
            body.exposed_inputs,
            body.exposed_outputs,
        )
        if body.exposed_inputs is not None:
            svc.exposed_inputs = [p.model_dump(exclude_none=True) for p in body.exposed_inputs]
        if body.exposed_outputs is not None:
            svc.exposed_outputs = [p.model_dump(exclude_none=True) for p in body.exposed_outputs]
    try:
        await session.commit()
    except IntegrityError:  # 改名并发撞唯一约束(TOCTOU 兜底)
        await session.rollback()
        raise HTTPException(409, detail=f"service name '{body.name}' already exists")
    await session.refresh(svc)
    invalidate("services")
    return svc


def _registry_of(request: Request):
    """从 app.state 拿 ModelRegistry(没有 lifespan 的测试态返回 None)。"""
    mm = getattr(request.app.state, "model_manager", None)
    return getattr(mm, "_registry", None) if mm is not None else None


async def _load_svc_with_snapshot(session: AsyncSession, service_id: int) -> ServiceInstance:
    """取服务并 undefer snapshot —— 枚举模型依赖必须读它,lazy load 在 async 路径会炸。"""
    svc = await session.scalar(
        select(ServiceInstance)
        .options(undefer(ServiceInstance.workflow_snapshot))
        .where(ServiceInstance.id == service_id)
    )
    if svc is None:
        raise HTTPException(404, detail="service not found")
    return svc


@router.get(
    "/services/{service_id}/autostart-preview",
    response_model=AutostartPreviewOut,
    dependencies=[Depends(require_admin)],
)
async def autostart_preview(
    service_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    """开机启动确认框的数据源:开了之后开机会加载哪些模型。

    只列 registry engine —— 图像类服务引用的是组件文件(dtype/device/lora 才定 combo),
    开机不预加载它们,所以也不在这里承诺。清单可能为空(那就是「开了也不会加载什么」)。
    """
    svc = await _load_svc_with_snapshot(session, service_id)
    return AutostartPreviewOut(
        service_id=str(svc.id),
        name=svc.name,
        autostart=bool(svc.autostart),
        preload_models=[
            PreloadModelOut(**m) for m in preload_model_infos(svc, _registry_of(request))
        ],
    )


@router.post(
    "/services/{service_id}/autostart",
    response_model=ServiceAutostartOut,
    dependencies=[Depends(require_admin)],
)
async def set_autostart(
    service_id: int,
    body: AutostartBody,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    """开/关服务的开机启动。开着 = 开机在 resident preload 之后顺带加载它引用的模型。"""
    svc = await _load_svc_with_snapshot(session, service_id)
    svc.autostart = body.enabled
    await session.commit()
    await session.refresh(svc)
    invalidate("services")
    logger.info(
        "service %s (%s) autostart → %s", svc.id, svc.name, "on" if body.enabled else "off",
    )
    out = ServiceAutostartOut.model_validate(svc)
    out.models = _service_model_refs(svc)
    out.preload_models = [
        PreloadModelOut(**m) for m in preload_model_infos(svc, _registry_of(request))
    ]
    return out


def _validate_exposed_against_snapshot(
    snapshot: dict,
    inputs: list[ExposedParam] | None,
    outputs: list[ExposedParam] | None,
) -> None:
    """Same hard contract as publish: every exposed.node_id must resolve in
    the frozen snapshot, and image-node outputs may only reference fields the
    node actually emits. Helpers now live in services.workflow_snapshot (纯模块),
    top-level import 不再循环。"""
    valid_ids = _node_ids(snapshot)
    types_by_id = _node_types_by_id(snapshot)
    for kind, params in (("input", inputs), ("output", outputs)):
        for p in params or []:
            if str(p.node_id) not in valid_ids:
                raise HTTPException(
                    422,
                    detail=(
                        f"exposed {kind} references node_id {p.node_id!r} "
                        f"that does not exist in the workflow snapshot"
                    ),
                )
    for p in outputs or []:
        node_type = types_by_id.get(str(p.node_id))
        if node_type not in _IMAGE_NODE_TYPES:
            continue
        field = p.input_name
        if field and field not in _IMAGE_OUTPUT_FIELDS:
            raise HTTPException(
                422,
                detail=(
                    f"exposed output input_name={field!r} is not emitted by "
                    f"{node_type}; allowed: {sorted(_IMAGE_OUTPUT_FIELDS)}"
                ),
            )


@router.delete(
    "/services/{service_id}",
    status_code=204,
    dependencies=[Depends(require_admin)],
)
async def delete_service(
    service_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    svc = await session.get(ServiceInstance, service_id)
    if svc is None:
        raise HTTPException(404, detail="service not found")
    wf_id = svc.workflow_id
    # 「模板即服务」是 1:1 建的(POST /api/v1/comfy-templates 同时建两行),删除也要
    # 对称 —— 否则每删一个桥服务就漏一行 comfy_templates,而且那行之后再也删不掉
    # (旧的 delete_template 要求配对服务存在)。2026-08-31 实机漏了 16 行。
    if svc.source_type == "comfy_template" and svc.source_id is not None:
        from src.models.comfy_template import ComfyTemplate  # noqa: PLC0415
        tpl = await session.get(ComfyTemplate, svc.source_id)
        if tpl is not None:
            await session.delete(tpl)
    await session.delete(svc)
    await session.flush()

    # 删服务后:若源工作流已无任何关联服务,把 status 退回 "draft"(镜像 /unpublish)。
    # 之前漏了这步 → 工作流卡在 status="published" 但无关联服务,卡片头部"已发布"绿徽章
    # 与底部"未关联服务"+发布按钮语义冲突(用户反馈)。
    if wf_id is not None:
        still = await session.execute(
            select(ServiceInstance.id).where(ServiceInstance.workflow_id == wf_id).limit(1)
        )
        if still.first() is None:
            wf = await session.get(Workflow, wf_id)
            if wf is not None and wf.status == "published":
                wf.status = "draft"

    await session.commit()
    # 工作流 status 可能翻回 draft → workflows 列表缓存也要失效。
    invalidate("services", "workflows")
