"""Task 10:predictions 端点加 admin-session 认证旁路(镜像 apps.py `_auth_apps_run`)。

背景:`/v1/services/{name}/predictions` 一直只认 `verify_bearer_token_any`(纯 Bearer),
但 Task 10 要求管理后台 Playground 对 `comfy_template` 服务走 respond-async 提交 —— 而
Playground 用的是 admin session cookie,不是外部 Bearer key。`/v1/apps/{name}/run`(现有同步
运行路径)早就有这个旁路(`_auth_apps_run`:无 Authorization header 时退回
`request_is_authed(request)`,通过则以 `(None, None)` 放行,下游按 admin 路径跳过
grant/限流/IDOR)。本文件验证 predictions 的 create/get/cancel 三端点同样加上这条旁路,
且不影响既有纯 Bearer 客户端路径(`test_prediction_e2e.py` 覆盖)。
"""
from __future__ import annotations

import asyncio

import pytest

from src.models.database import get_session_factory
from src.services.nodes import comfy_bridge as nb
from tests.comfy.test_bridge_node import FakeClient

WF = {
    "138": {"class_type": "T", "inputs": {"value": ""}},
    "92": {"class_type": "SaveVideo", "inputs": {}},
}
MAPPING = {"exposed_params": [
    {"key": "prompt", "label": "提示词", "type": "string",
     "comfy_node_id": "138", "comfy_input": "value", "required": True},
]}


async def _make_service(client, name: str) -> None:
    r = await client.post("/api/v1/comfy-templates", json={"name": name, "workflow": WF})
    assert r.status_code == 201, r.text
    tid = int(r.json()["id"])
    r2 = await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=MAPPING)
    assert r2.status_code == 200, r2.text


def _patch_no_thumbnail(monkeypatch) -> None:
    async def _no_thumbnail(_path):
        return None
    monkeypatch.setattr(nb, "extract_first_frame", _no_thumbnail)


async def _poll_prediction(client, pid, timeout: float = 5.0) -> dict:
    """无 Authorization header —— admin session 旁路轮询。"""
    deadline = asyncio.get_event_loop().time() + timeout
    body: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/predictions/{pid}")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("succeeded", "failed", "canceled"):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"prediction {pid} 未在 {timeout}s 内到终态,last={body}")


@pytest.mark.asyncio
async def test_create_prediction_without_bearer_uses_admin_session(client, monkeypatch):
    """管理后台 Playground 场景:无 Authorization header,respond-async 提交靠 admin
    session 旁路放行(测试环境 ADMIN_PASSWORD="" → request_is_authed 恒真)。"""
    fc = FakeClient()
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    _patch_no_thumbnail(monkeypatch)

    await _make_service(client, "nous-admin-e2e-video")

    r = await client.post(
        "/v1/services/nous-admin-e2e-video/predictions", json={"input": {"prompt": "hi"}},
        headers={"Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    pid = r.json()["id"]

    final = await _poll_prediction(client, pid)
    assert final["status"] == "succeeded", final
    assert "mp4" in str(final["output"])


# 真 ComfyUI 被 `/interrupt` 打断后,`GET /history/{id}` 那条记录里 `outputs` 是空的
# (渲染没走到写产物的输出节点),`status.status_str` 是 "error"、`completed` False。
# `ComfyClient.wait()` 原样返回这个 dict —— 桥节点的 `collect_outputs` 因此收不到任何
# 产物,抛 ComfyError("ComfyUI 未产出任何产物(可能被中断)")。测试替身必须复刻这个
# 形状,否则 "interrupt 之后 wait 仍然返回成功" 是个真机不存在的场景。
INTERRUPTED_HISTORY = {
    "outputs": {},
    "status": {"status_str": "error", "completed": False,
               "messages": [["execution_interrupted", {}]]},
}


def _gated_client(gate: asyncio.Event, *, wait_result: dict | None = None):
    """FakeClient,但 `wait()` 堵在 `gate` 上、`interrupt()` 放行。

    `wait_result=None` → 放行后返回 `INTERRUPTED_HISTORY`(真客户端语义)。
    传 dict 则返回它(用来构造"取消之后节点还是报成功"的赛跑)。
    """

    class _GatedClient(FakeClient):
        async def wait(self, prompt_id, *, timeout_s, interval_s=2.0, **_kw):
            await gate.wait()
            if wait_result is not None:
                return wait_result
            return INTERRUPTED_HISTORY

        async def interrupt(self) -> None:
            gate.set()

    return _GatedClient()


async def _start_and_await_render(client, monkeypatch, fc, name: str) -> str:
    """建服务 → respond-async 提交 → 等到桥节点真正持有渲染信号量,返回 prediction id。"""
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    import src.api.routes.comfy_templates as comfy_templates_route
    monkeypatch.setattr(comfy_templates_route, "get_client", lambda: fc)
    _patch_no_thumbnail(monkeypatch)

    await _make_service(client, name)

    r = await client.post(
        f"/v1/services/{name}/predictions", json={"input": {"prompt": "hi"}},
        headers={"Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    pid = r.json()["id"]

    deadline = asyncio.get_event_loop().time() + 5.0
    status = None
    while asyncio.get_event_loop().time() < deadline:
        status = (await client.get(f"/v1/predictions/{pid}")).json()["status"]
        if status == "processing":
            break
        await asyncio.sleep(0.02)
    assert status == "processing", f"prediction 未在超时前进入 running(last={status})"
    # C1 fix:cancel_prediction 现在按 comfy_bridge.get_running_task_id() 精确匹配才转发
    # interrupt——ExecutionTask.status 在节点真正拿到渲染信号量*之前*就已经是 "running"
    # 了,轮询 DB 状态不能证明节点已经到那一步。直接轮询真实条件(确定性,不猜调度轮次)。
    deadline2 = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline2:
        if nb.get_running_task_id() == int(pid):
            break
        await asyncio.sleep(0.005)
    else:
        raise AssertionError(f"渲染信号量没能在超时前被 task {pid} 持有")
    return pid


async def _await_runner_finished(pid: str, timeout: float = 5.0) -> str:
    """等后台 `run_workflow_task` 真正收尾完,返回 ExecutionTask.status(DB 原值)。

    不能只等 status 到终态 —— cancel 端点自己就把 status 写成了 cancelled,那样会在
    runner 还没收尾时就返回,恰好放过"runner 事后把 cancelled 改写成 failed/completed"
    这个要测的场景。`duration_ms` 只有 runner 的三条收尾路径才写,拿它当完成信号。
    """
    from src.models.execution_task import ExecutionTask
    session_factory = get_session_factory()
    deadline = asyncio.get_event_loop().time() + timeout
    snap: tuple[str | None, int | None] = (None, None)
    while asyncio.get_event_loop().time() < deadline:
        async with session_factory() as s:
            task = await s.get(ExecutionTask, int(pid))
            snap = (task.status, task.duration_ms) if task is not None else (None, None)
        if snap[1] is not None:
            return snap[0]
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {pid} 的 runner 未在 {timeout}s 内收尾,last={snap}")


@pytest.mark.asyncio
async def test_cancel_prediction_without_bearer_uses_admin_session(client, monkeypatch):
    """管理后台 Playground 场景的取消:无 Authorization header 走 admin session 旁路,
    对卡在渲染上的 comfy_template prediction 转发 `/interrupt` 并落 canceled。

    替身按真客户端语义走:`/interrupt` 之后 `wait()` 返回的是**中断形** history
    (`outputs` 空),桥节点因此抛 ComfyError —— 不是原来那种"interrupt 后还返回成功
    结果"的真机不存在时序(那正是这条用例在 CI 上偶发翻车的地方之一)。
    """
    fc = _gated_client(asyncio.Event())
    pid = await _start_and_await_render(client, monkeypatch, fc, "nous-admin-e2e-cancel")

    cancel_resp = await client.post(f"/v1/predictions/{pid}/cancel")
    assert cancel_resp.status_code == 200, cancel_resp.text
    assert cancel_resp.json()["status"] == "canceled"
    # 被 interrupt 的渲染收尾(桥节点抛"未产出任何产物")后,终态仍是 cancelled ——
    # 不能被 workflow_runner 的 except 路径改写成 failed。
    assert await _await_runner_finished(pid) == "cancelled"
    assert (await client.get(f"/v1/predictions/{pid}")).json()["status"] == "canceled"


@pytest.mark.asyncio
async def test_cancel_terminal_state_survives_late_node_success(client, monkeypatch):
    """终态不被后到的成功覆盖 —— 直接针对 CI flake(#33708220300)那条赛跑的回归。

    根因是**产品侧顺序**:旧 `cancel_prediction` 先 `await interrupt()`、再条件 UPDATE
    落 cancelled。interrupt 让出事件循环(生产里是最长 5s 的真实 HTTP 往返)期间,渲染
    协程醒来一路跑完,`workflow_runner` 抢先把终态写成 completed,cancel 的条件 UPDATE
    只匹配 queued/running → 0 行 → 端点回 `succeeded`,取消凭空消失。

    这里刻意让替身在 interrupt 之后**仍返回成功的 history**(最不利排序:节点完全没
    感知到中断),断言取消依然赢:cancel 必须在放行渲染*之前*就把 cancelled 落库,
    runner 的收尾则必须 honor 已经存在的 cancelled。
    """
    fc = _gated_client(asyncio.Event(), wait_result={"outputs": {"92": {"images": [
        {"filename": "out.mp4", "subfolder": "", "type": "output"}]}}})
    pid = await _start_and_await_render(client, monkeypatch, fc, "nous-admin-e2e-cancel-race")

    cancel_resp = await client.post(f"/v1/predictions/{pid}/cancel")
    assert cancel_resp.status_code == 200, cancel_resp.text
    assert cancel_resp.json()["status"] == "canceled"
    assert await _await_runner_finished(pid) == "cancelled"
    assert (await client.get(f"/v1/predictions/{pid}")).json()["status"] == "canceled"


@pytest.mark.asyncio
async def test_create_prediction_falls_back_to_admin_session_when_bearer_invalid(client, monkeypatch):
    """I5 fix:`_auth_predictions` 收到一个 `Authorization` header,但它不是任何已注册
    `InstanceApiKey`(典型场景是 CLI `ADMIN_TOKEN` bearer——它压根不是 M:N key)时,
    `verify_bearer_token_any` 会抛 401。修复前这个异常直接甩出去,ADMIN_TOKEN 永远够
    不到 predictions 端点;修复后应退回 `request_is_authed` 再判一次。

    注:conftest 强制 `ADMIN_PASSWORD=""` ⇒ `is_login_required()`==False ⇒
    `request_is_authed` 对任何请求恒真——这里没法真正验证 ADMIN_TOKEN 的值本身被比对
    上了,只能验证"bearer 校验失败时会走 fallback 而不是硬 401"这条路径确实被触发
    (旧代码在这个请求上会 401,新代码走 fallback 落到 202)。"""
    fc = FakeClient()
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    _patch_no_thumbnail(monkeypatch)

    await _make_service(client, "nous-admin-token-fallback")

    r = await client.post(
        "/v1/services/nous-admin-token-fallback/predictions", json={"input": {"prompt": "hi"}},
        headers={"Authorization": "Bearer sk-not-a-real-instance-key", "Prefer": "respond-async"})
    assert r.status_code == 202, r.text


@pytest.mark.asyncio
async def test_create_prediction_task_has_no_owner_key_on_admin_path(client, monkeypatch):
    """admin 路径不消费任何 InstanceApiKey 配额 —— ExecutionTask.api_key_id 落 NULL
    (镜像 apps.py 的 admin_run:「admin 隐式授权,跳过 quota」)。"""
    fc = FakeClient()
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    _patch_no_thumbnail(monkeypatch)

    await _make_service(client, "nous-admin-e2e-owner")

    r = await client.post(
        "/v1/services/nous-admin-e2e-owner/predictions", json={"input": {"prompt": "hi"}},
        headers={"Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    pid = int(r.json()["id"])

    from src.models.execution_task import ExecutionTask
    session_factory = get_session_factory()
    async with session_factory() as s:
        task = await s.get(ExecutionTask, pid)
        assert task.api_key_id is None
