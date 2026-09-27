"""模板即服务的删除对称性 —— 删任一边都不该留下孤儿。

`POST /api/v1/comfy-templates` **同时**建 `comfy_templates` 行和配对的
`ServiceInstance`(CLAUDE.md「模板即服务」),两者 1:1。但删除路径原先不对称:

  DELETE /api/v1/services/{id}          只删 ServiceInstance → 模板变孤儿
  DELETE /api/v1/comfy-templates/{id}   要求配对服务存在,否则 404
                                        → 孤儿模板**再也删不掉**

2026-08-31 实机:清理 16 个测试残留时先删了服务,回头删模板全部 404
"service not found for template",行卡在库里没有任何 API 能清。
"""
import pytest

WF = {"138": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": ""}},
      "92": {"class_type": "SaveImage", "inputs": {}}}


async def _mk(client, name):
    r = await client.post("/api/v1/comfy-templates", json={"name": name, "workflow": WF})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _template_ids(client):
    r = await client.get("/api/v1/comfy-templates")
    assert r.status_code == 200, r.text
    body = r.json()
    items = body if isinstance(body, list) else (body.get("templates") or body.get("items") or [])
    return {str(t["id"]) for t in items}


@pytest.mark.asyncio
async def test_deleting_template_removes_both(client):
    """基线:正常删模板 → 模板和服务一起没。"""
    tid = await _mk(client, "nous-life-both")
    assert (await client.delete(f"/api/v1/comfy-templates/{tid}")).status_code == 204
    assert str(tid) not in await _template_ids(client)
    assert (await client.get("/v1/services/nous-life-both/schema")).status_code == 404


@pytest.mark.asyncio
async def test_deleting_service_also_removes_its_template(client):
    """删桥服务要连带删模板 —— 否则每删一个桥服务就漏一行 comfy_templates。"""
    tid = await _mk(client, "nous-life-svc")
    svc = (await client.get("/api/v1/services")).json()
    items = svc if isinstance(svc, list) else (svc.get("services") or svc.get("items") or [])
    sid = next(s["id"] for s in items if s["name"] == "nous-life-svc")

    assert (await client.delete(f"/api/v1/services/{sid}")).status_code == 204
    assert str(tid) not in await _template_ids(client), "模板变成孤儿了"


@pytest.mark.asyncio
async def test_orphan_template_is_still_deletable(client):
    """兜底:即便配对服务已经不在(历史遗留的孤儿行),删模板也必须能成功,
    不能 404 —— 否则那些行没有任何 API 清得掉。"""
    from sqlalchemy import delete as sa_delete

    from src.models.database import get_session_factory
    from src.models.service_instance import ServiceInstance

    tid = await _mk(client, "nous-life-orphan")
    # 绕过 API 直接摘掉服务,人为造一个孤儿模板
    async with get_session_factory()() as s:
        await s.execute(sa_delete(ServiceInstance).where(
            ServiceInstance.source_type == "comfy_template",
            ServiceInstance.source_id == int(tid)))
        await s.commit()

    assert (await client.delete(f"/api/v1/comfy-templates/{tid}")).status_code == 204
    assert str(tid) not in await _template_ids(client)
