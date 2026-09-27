"""数据面「鉴权 + 按名解析服务」的共用实现(spec 2026-09-26 skill-runs §4)。

`/v1/services/{name}/predictions` 与 `/v1/skill-runs/*` 共用,新路由不 import 别的路由模块的私有函数。
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_auth import enforce_instance_rate_limit, verify_bearer_token_any
from src.models.database import get_async_session
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance
from src.services.model_resolver import ModelNotFound, resolve_target_service

Auth = tuple[ServiceInstance | None, InstanceApiKey | None]


async def auth_bearer_or_admin_session(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> Auth:
    """Bearer-token auth,带 admin-session 旁路(Task 10:Playground 异步运行态)。

    镜像 `apps.py::_auth_apps_run`:外部 Bearer 客户端走完整 key 校验(优先);Playground
    对 `comfy_template` 服务走 respond-async 提交/轮询/取消时用的是 admin session cookie,
    不是 Bearer key —— 原先三端点的 `Authorization: Header(...)` 是必填,FastAPI 会在
    header 校验阶段就以「Field required」拒绝,同 apps.py 改前的坑。返回 `(None, None)`
    让下游(`resolve_service_for_call` / get_prediction / cancel_prediction)按 admin 路径跳过
    grant/限流/IDOR owner 校验(单管理员部署里 admin 隐式对所有 prediction 有权限)。

    I5 fix:`Authorization` header 存在但不是任何已注册 `InstanceApiKey`(典型场景——
    CLI `ADMIN_TOKEN` bearer,它根本不是 M:N key)时,`verify_bearer_token_any` 会抛
    401。旧代码在这里直接把异常甩出去,`ADMIN_TOKEN` 永远够不到 predictions 端点。
    现在 bearer 校验失败就退回 `request_is_authed`(它自己会看 `Authorization` header
    里的 `ADMIN_TOKEN` bearer,见 admin_session.py::admin_token_matches),校验通过
    仍走 `(None, None)` admin 旁路——IDOR 语义不变。
    """
    from src.api.admin_session import request_is_authed  # noqa: PLC0415
    if authorization:
        try:
            return await verify_bearer_token_any(authorization, session)
        except HTTPException:
            if request_is_authed(request):
                return None, None
            raise
    if request_is_authed(request):
        return None, None
    raise HTTPException(401, detail="Missing API key or admin session")


async def resolve_service_for_call(
    session: AsyncSession, auth: Auth, name: str,
) -> tuple[ServiceInstance, InstanceApiKey | None]:
    """bearer key(或 admin session)→ 目标服务(URL 的 {name}),校验 active + 加载 deferred 列。

    admin 路径(`api_key is None`,Task 10 旁路)跳过 grant/限流 —— 与 `apps.py`
    的 `admin_run` 分支一致:单管理员部署里 admin 隐式对所有服务有权限。
    其余一律按 M:N grant 解析:legacy 1:1 绑定 key 已随 legacy rip 删除,
    `verify_bearer_token_any` 恒返回 `(None, key)`(`InstanceApiKey.instance_id` 仅为
    schema 稳定保留,不再授予任何访问)。
    """
    _, api_key = auth
    if api_key is None:  # admin session 旁路:直接按 name 查活跃服务,不涉及任何 key
        stmt = select(ServiceInstance).where(ServiceInstance.name == name)
        instance = (await session.execute(stmt)).scalar_one_or_none()
        if instance is None:
            raise HTTPException(404, detail="service not found")
    else:  # M:N key:按 URL name 解析授权 + 限流(verify 没做)
        try:
            instance = await resolve_target_service(session, api_key=api_key, requested_model=name)
        except ModelNotFound as e:
            raise HTTPException(404, detail=str(e)) from e
        if instance.name != name:
            raise HTTPException(403, detail="API key not authorized for this service")
    if instance.status != "active":
        raise HTTPException(403, detail="service is inactive")
    if api_key is not None:
        await enforce_instance_rate_limit(instance)
    await session.refresh(
        instance, attribute_names=["workflow_snapshot", "exposed_inputs", "exposed_outputs"])
    return instance, api_key
