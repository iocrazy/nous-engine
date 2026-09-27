"""Task 7: predictions 端到端 + 取消打通。

comfy_template 服务(Task 3)+ 桥节点(Task 6)经统一 `POST /v1/services/{name}/predictions`
（服务层 API spec PR-2,见 `src/api/routes/predictions.py`）真正跑通:respond-async → 202 →
轮询 `/v1/predictions/{id}` → succeeded + 视频 url。cancel 打通 `POST /v1/predictions/{id}/cancel`
→ comfy_template 服务 running 态时额外转发 `ComfyClient.interrupt()`(Task 7 本文件新增行为,
见 `predictions.py:cancel_prediction`)。

鉴权:`/v1/*` 走 `verify_bearer_token_any`,legacy 1:1 key 已删(见
`tests/test_prediction_service_pr2.py`),只认 M:N `InstanceApiKey` + `ApiKeyGrant`——本文件
的 `_make_service_and_key` 照抄该文件 `prediction_service` fixture 的建 key/建 grant 方式。
DB 用 `client` fixture 背后进程内 memoized 的共享 session factory(同
`test_bridge_node.py::test_load_template_real_db_lookup`),不是自建临时 engine。
"""
from __future__ import annotations

import asyncio
import secrets as _secrets

import bcrypt
import pytest
from sqlalchemy import select

import src.api.routes.comfy_templates as comfy_templates_route
import src.services.nodes.comfy_bridge as nb
from src.models.api_gateway import ApiKeyGrant
from src.models.database import get_session_factory
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance
from src.services.comfy.client import ComfyError
from tests.comfy.test_bridge_node import FakeClient

WF = {
    "138": {"class_type": "T", "inputs": {"value": ""}},
    "92": {"class_type": "SaveVideo", "inputs": {}},
}
MAPPING = {"exposed_params": [
    {"key": "prompt", "label": "提示词", "type": "string",
     "comfy_node_id": "138", "comfy_input": "value", "required": True},
]}


async def _make_service_and_key(client, name: str) -> str:
    """建一个 comfy_template 服务(经 API,含 mapping)+ 一把 M:N bearer key 授权它。返回原始 key。"""
    r = await client.post("/api/v1/comfy-templates", json={"name": name, "workflow": WF})
    assert r.status_code == 201, r.text
    tid = int(r.json()["id"])
    r2 = await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=MAPPING)
    assert r2.status_code == 200, r2.text

    raw = f"sk-comfy-{_secrets.token_hex(8)}"
    session_factory = get_session_factory()
    async with session_factory() as s:
        svc = (await s.execute(
            select(ServiceInstance).where(ServiceInstance.name == name))).scalar_one()
        key = InstanceApiKey(
            instance_id=None, label="t",  # M:N — 走 grant(legacy 1:1 已删)
            key_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
            key_prefix=raw[:10], is_active=True)
        s.add(key)
        await s.commit()
        await s.refresh(key)
        s.add(ApiKeyGrant(api_key_id=key.id, service_id=svc.id, status="active"))
        await s.commit()
    return raw


async def _poll_prediction(client, pid, headers, timeout: float = 5.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    body: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/predictions/{pid}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("succeeded", "failed", "canceled"):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"prediction {pid} 未在 {timeout}s 内到终态,last={body}")


def _assert_video_output_shape(output: dict) -> None:
    """Upgrade of the old loose `"mp4" in str(output)` check (reviewer
    recommendation): asserts the actual envelope shape —
    `output["outputs"]["out"]` (the bridge snapshot's terminal `video_output`
    node, see comfy_templates.py::_bridge_snapshot) carries a non-empty
    `video_url` AND an `items` list whose entries carry both `url` and `kind`
    (not just "some string containing mp4 somewhere in the whole payload").
    """
    out = (output or {}).get("outputs", {}).get("out", {})
    assert out.get("video_url"), output
    assert ".mp4" in out["video_url"], output
    items = out.get("items")
    assert items, output
    for item in items:
        assert item.get("url"), item
        assert item.get("kind"), item
    assert items[0]["kind"] == "video"
    assert items[0]["url"] == out["video_url"]


async def _wait_running_task_id(task_id: int, timeout: float = 5.0) -> None:
    """C1 fix follow-up: `cancel_prediction` now forwards `/interrupt` only when
    the cancelled prediction_id matches `comfy_bridge.get_running_task_id()`
    (see predictions.py) — but `ExecutionTask.status` flips to "running" in
    `workflow_runner.run_workflow_task` *before* the bridge node ever reaches
    `async with _SEM:` and sets that module global. Polling until DB status ==
    "processing" is therefore not by itself proof the node has acquired the
    semaphore yet. Poll the actual condition directly (deterministic — no
    guessing at scheduling-turn counts) before asserting on interrupt-
    forwarding behavior."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if nb.get_running_task_id() == task_id:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(
        f"comfy_bridge 的渲染信号量没能在 {timeout}s 内被 task {task_id} 持有"
        f"(last get_running_task_id()={nb.get_running_task_id()!r})")


def _patch_no_thumbnail(monkeypatch) -> None:
    """跳过真 ffmpeg——FakeClient.download() 吐的不是合法 mp4 字节,extract_first_frame
    会静默 fail(设计如此,不影响本文件断言),但真去 spawn 一个进程没必要拖慢测试。"""
    async def _no_thumbnail(_path):
        return None
    monkeypatch.setattr(nb, "extract_first_frame", _no_thumbnail)


@pytest.mark.asyncio
async def test_async_prediction_completes_with_video(client, monkeypatch):
    fc = FakeClient()
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    _patch_no_thumbnail(monkeypatch)

    raw_key = await _make_service_and_key(client, "nous-e2e-video")
    headers = {"Authorization": f"Bearer {raw_key}"}

    r = await client.post(
        "/v1/services/nous-e2e-video/predictions", json={"input": {"prompt": "hi"}},
        headers={**headers, "Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    pid = r.json()["id"]

    final = await _poll_prediction(client, pid, headers)
    assert final["status"] == "succeeded", final
    _assert_video_output_shape(final["output"])


@pytest.mark.asyncio
async def test_predictions_sync_mode_also_returns_video(client, monkeypatch):
    """无 Prefer(同步阻塞)路径同样跑通——回归 predictions.py 的两条模式共享同一执行链路。"""
    fc = FakeClient()
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    _patch_no_thumbnail(monkeypatch)

    raw_key = await _make_service_and_key(client, "nous-e2e-video-sync")
    headers = {"Authorization": f"Bearer {raw_key}"}

    r = await client.post(
        "/v1/services/nous-e2e-video-sync/predictions", json={"input": {"prompt": "hi"}},
        headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded", body
    _assert_video_output_shape(body["output"])


class _InterruptFakeClient(FakeClient):
    """FakeClient 子类:wait() 卡在测试控制的 asyncio.Event 上(不用 sleep 赌时序),
    interrupt() 计数供 cancel 转发断言。"""

    def __init__(self, gate: asyncio.Event):
        super().__init__()
        self._gate = gate
        self.interrupt_calls = 0

    async def wait(self, prompt_id, *, timeout_s, interval_s=2.0, **_kw):
        await self._gate.wait()
        return await super().wait(
                prompt_id, timeout_s=timeout_s, interval_s=interval_s, **_kw)

    async def interrupt(self) -> None:
        self.interrupt_calls += 1


@pytest.mark.asyncio
async def test_cancel_forwards_interrupt_and_reaches_cancelled(client, monkeypatch):
    """取消一个卡在 ComfyClient.wait() 上的 running prediction:
    - cancel 端点(predictions.py cancel_prediction)在发现 task 归属服务是 comfy_template
      且 status=running 时,额外 await get_client().interrupt()(get_client 从
      comfy_templates 路由模块 import——同一个被 monkeypatch 的 FakeClient 实例)。
    - 放行卡住的 wait() 后,workflow_runner 的 cancel-race guard 必须 honor 已经落的
      cancelled,而不是被"成功跑完"的路径覆盖回 succeeded。
    """
    gate = asyncio.Event()
    fc = _InterruptFakeClient(gate)
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    monkeypatch.setattr(comfy_templates_route, "get_client", lambda: fc)
    _patch_no_thumbnail(monkeypatch)

    raw_key = await _make_service_and_key(client, "nous-e2e-cancel")
    headers = {"Authorization": f"Bearer {raw_key}"}

    r = await client.post(
        "/v1/services/nous-e2e-cancel/predictions", json={"input": {"prompt": "hi"}},
        headers={**headers, "Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    pid = r.json()["id"]

    # 等后台任务真正进 running(= 已提交 ComfyUI,卡在 gate 上)——interrupt 转发只在
    # running 态触发,过早 cancel(还 queued)测的就不是这条分支了。
    deadline = asyncio.get_event_loop().time() + 5.0
    status = None
    while asyncio.get_event_loop().time() < deadline:
        status = (await client.get(f"/v1/predictions/{pid}", headers=headers)).json()["status"]
        if status == "processing":
            break
        await asyncio.sleep(0.02)
    assert status == "processing", f"prediction 未在超时前进入 running(last={status})"
    await _wait_running_task_id(int(pid))  # see docstring — C1 fix needs the node past `async with _SEM:`

    cancel_resp = await client.post(f"/v1/predictions/{pid}/cancel", headers=headers)
    assert cancel_resp.status_code == 200, cancel_resp.text
    assert cancel_resp.json()["status"] == "canceled"
    assert fc.interrupt_calls == 1

    # 放行卡住的 wait()——run_workflow_task 的 success 路径会 refresh 看到 cancelled 并 honor。
    gate.set()
    final = await _poll_prediction(client, pid, headers)
    assert final["status"] == "canceled", final
    assert final["output"] is None  # 未终态成功,不该有 output


@pytest.mark.asyncio
async def test_cancel_does_not_clobber_a_render_that_already_finished(client, monkeypatch):
    """code review Important(TOCTOU)的另一半:渲染**已经**跑完并把 completed+result
    落库之后才到的 cancel,不能把这个成功的终态翻回 cancelled 且丢 output
    (task_to_prediction 只在 succeeded 时给 output)。

    2026-09-02 顺序修正后这条测试的装置变了(行为断言没变):以前是让
    `FakeClient.interrupt()` 在被 await 期间放行卡住的 wait() —— 因为旧
    `cancel_prediction` 先 interrupt 再落 cancelled,那个 await 窗口就是渲染赢下竞态的
    地方。现在 cancel 先把 cancelled 提交、再 interrupt,interrupt 已经不可能"制造"出
    一个后到的 completed 了(制造出来也会被 workflow_runner 的 honor-cancelled 挡掉,
    见 test_cancel_forwards_interrupt_and_reaches_cancelled)。真正还会发生的"渲染赢"
    只剩一种:渲染自己先跑完。装置照这个改 —— 放行 wait()、轮询等 completed 真落库,
    再发 cancel。

    另断言 `interrupt_calls == 0`:任务已终态,不该再往 sidecar 发 `/interrupt` ——
    那一发打不到本任务,只会误伤排在后面的下一个渲染(C1 那条不变式)。
    """
    from src.models.execution_task import ExecutionTask

    wait_gate = asyncio.Event()

    class _RaceFakeClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.interrupt_calls = 0

        async def wait(self, prompt_id, *, timeout_s, interval_s=2.0, **_kw):
            await wait_gate.wait()
            return await super().wait(
                prompt_id, timeout_s=timeout_s, interval_s=interval_s, **_kw)

        async def interrupt(self) -> None:
            self.interrupt_calls += 1

    fc = _RaceFakeClient()
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    monkeypatch.setattr(comfy_templates_route, "get_client", lambda: fc)
    _patch_no_thumbnail(monkeypatch)

    raw_key = await _make_service_and_key(client, "nous-e2e-cancel-race")
    headers = {"Authorization": f"Bearer {raw_key}"}

    r = await client.post(
        "/v1/services/nous-e2e-cancel-race/predictions", json={"input": {"prompt": "hi"}},
        headers={**headers, "Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    pid = r.json()["id"]

    deadline = asyncio.get_event_loop().time() + 5.0
    status = None
    while asyncio.get_event_loop().time() < deadline:
        status = (await client.get(f"/v1/predictions/{pid}", headers=headers)).json()["status"]
        if status == "processing":
            break
        await asyncio.sleep(0.02)
    assert status == "processing", f"prediction 未在超时前进入 running(last={status})"
    await _wait_running_task_id(int(pid))  # see docstring — C1 fix needs the node past `async with _SEM:`

    # 放行渲染,轮询等 run_workflow_task 真把 completed 落库(不赌 sleep 时长)。
    wait_gate.set()
    sf = get_session_factory()
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        async with sf() as s:
            t = await s.get(ExecutionTask, int(pid))
        if t is not None and t.status == "completed":
            break
        await asyncio.sleep(0.005)
    else:
        raise AssertionError("后台渲染没能在超时前落 completed(测试装置问题)")

    cancel_resp = await client.post(f"/v1/predictions/{pid}/cancel", headers=headers)
    assert cancel_resp.status_code == 200, cancel_resp.text
    assert fc.interrupt_calls == 0

    # 竞态赢家是「渲染先完成」——cancel 的响应必须如实反映 succeeded + output,不能报
    # cancelled 丢 output。
    body = cancel_resp.json()
    assert body["status"] == "succeeded", body
    assert body["output"] is not None and "mp4" in str(body["output"]), body

    # DB 落态同样必须是 completed,不是被 cancel 路径的 UPDATE 翻写成 cancelled。
    async with get_session_factory()() as s:
        t = await s.get(ExecutionTask, int(pid))
        assert t.status == "completed", t.status


@pytest.mark.asyncio
async def test_cancel_on_non_comfy_service_skips_interrupt(client, monkeypatch):
    """非 comfy_template 服务(普通 workflow 服务)cancel 时不该去碰 comfy get_client——
    回归守护:cancel_prediction 的新分支必须按 ServiceInstance.source_type 精确判断,
    不是对所有 running task 一律尝试 interrupt。"""
    from src.models.execution_task import ExecutionTask

    session_factory = get_session_factory()
    async with session_factory() as s:
        inst = ServiceInstance(
            source_type="workflow", source_name="plain-wf", name="plain-svc",
            type="workflow", status="active",
            workflow_snapshot={"nodes": {}, "edges": []},
            exposed_inputs=[], exposed_outputs=[],
        )
        s.add(inst)
        await s.commit()
        await s.refresh(inst)
        raw = f"sk-plain-{_secrets.token_hex(8)}"
        key = InstanceApiKey(
            instance_id=None, label="t",
            key_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
            key_prefix=raw[:10], is_active=True)
        s.add(key)
        await s.commit()
        await s.refresh(key)
        s.add(ApiKeyGrant(api_key_id=key.id, service_id=inst.id, status="active"))
        await s.commit()

        task = ExecutionTask(
            workflow_id=inst.source_id, api_key_id=key.id, workflow_name=inst.name,
            status="running", nodes_total=1,
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        task_id = task.id

    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("comfy_templates.get_client 不该在非 comfy_template 服务上被调用")
    monkeypatch.setattr(comfy_templates_route, "get_client", _boom)

    resp = await client.post(f"/v1/predictions/{task_id}/cancel",
                             headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "canceled"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_empty_outputs_marks_task_failed(client, monkeypatch):
    """C1 fix companion:ComfyUI 返回了 history 但没有一个可识别产物(渲染被中断在
    写产物之前是最常见成因)→ 桥节点报错,task 必须落 failed(不是悄悄 succeeded 却
    没有任何输出)。"""
    class _EmptyOutputsFakeClient(FakeClient):
        async def wait(self, prompt_id, *, timeout_s, interval_s=2.0, **_kw):
            return {"outputs": {}}

    fc = _EmptyOutputsFakeClient()
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    _patch_no_thumbnail(monkeypatch)

    raw_key = await _make_service_and_key(client, "nous-e2e-empty-outputs")
    headers = {"Authorization": f"Bearer {raw_key}"}

    r = await client.post(
        "/v1/services/nous-e2e-empty-outputs/predictions", json={"input": {"prompt": "hi"}},
        headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed", body
    assert "未产出" in (body["error"] or ""), body


@pytest.mark.asyncio
async def test_cancel_queued_behind_semaphore_does_not_interrupt_other_render(client, monkeypatch):
    """C1 fix — 核心场景:comfy_bridge._SEM 是进程级单例,不是每 prediction 一把。两个
    comfy_template prediction(A、B)先后提交,A 先抢到信号量正在渲染(卡在 wait()),
    B 排在信号量后面等待——此时 B 的 ExecutionTask.status 也已经是 "running"(
    workflow_runner 在拿到信号量*之前*就把 status 落 running 了)。

    - 取消 B:不该打断 A 正在跑的渲染(fc.interrupt_calls 仍是 0),B 必须在拿到信号量
      后经 cancel-race 复查直接终态 cancelled,从未调用 client.submit()。
    - 取消 A:此时 A 才是真正持有信号量、卡在 ComfyUI 上的任务,cancel 必须转发
      interrupt(fc.interrupt_calls 变 1)。
    """
    render_gate = asyncio.Event()

    class _TwoTaskFakeClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.interrupt_calls = 0
            self.submitted_graphs: list[dict] = []
            self.first_submitted = asyncio.Event()

        async def submit(self, graph):
            self.submitted_graphs.append(graph)
            self.first_submitted.set()
            return f"p{len(self.submitted_graphs)}"

        async def wait(self, prompt_id, *, timeout_s, interval_s=2.0, **_kw):
            await render_gate.wait()
            return {"outputs": {"92": {"images": [
                {"filename": "out.mp4", "subfolder": "", "type": "output"}]}}}

        async def interrupt(self) -> None:
            self.interrupt_calls += 1

    fc = _TwoTaskFakeClient()
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    monkeypatch.setattr(comfy_templates_route, "get_client", lambda: fc)
    _patch_no_thumbnail(monkeypatch)

    raw_key_a = await _make_service_and_key(client, "nous-e2e-2task-a")
    raw_key_b = await _make_service_and_key(client, "nous-e2e-2task-b")
    headers_a = {"Authorization": f"Bearer {raw_key_a}"}
    headers_b = {"Authorization": f"Bearer {raw_key_b}"}

    # A: submit → acquires the module-level semaphore, calls submit(), then blocks
    # in wait() on render_gate. Wait for fc.first_submitted so we know A truly
    # holds the semaphore before starting B (deterministic, not a sleep-and-hope).
    ra = await client.post(
        "/v1/services/nous-e2e-2task-a/predictions", json={"input": {"prompt": "a"}},
        headers={**headers_a, "Prefer": "respond-async"})
    assert ra.status_code == 202, ra.text
    pid_a = ra.json()["id"]
    await asyncio.wait_for(fc.first_submitted.wait(), timeout=5.0)

    # B: submitted while A holds the semaphore — B's node.invoke() will reach
    # `async with _SEM:` and block there (never calls client.submit()).
    rb = await client.post(
        "/v1/services/nous-e2e-2task-b/predictions", json={"input": {"prompt": "b"}},
        headers={**headers_b, "Prefer": "respond-async"})
    assert rb.status_code == 202, rb.text
    pid_b = rb.json()["id"]
    deadline = asyncio.get_event_loop().time() + 5.0
    status_b = None
    while asyncio.get_event_loop().time() < deadline:
        status_b = (await client.get(f"/v1/predictions/{pid_b}", headers=headers_b)).json()["status"]
        if status_b == "processing":
            break
        await asyncio.sleep(0.02)
    assert status_b == "processing", f"B 未在超时前进入 running(last={status_b})"
    # No extra wait needed here: A already provably holds the semaphore (we
    # waited for fc.first_submitted above, which only fires after A sets
    # `_running_task_id`), so `get_running_task_id() == pid_b` can never be
    # true right now regardless of how far B's own coroutine has progressed —
    # the interrupt-skip assertion below doesn't race on B's scheduling at all.

    # Cancel B first — must NOT touch A's render.
    cancel_b = await client.post(f"/v1/predictions/{pid_b}/cancel", headers=headers_b)
    assert cancel_b.status_code == 200, cancel_b.text
    assert cancel_b.json()["status"] == "canceled"
    assert fc.interrupt_calls == 0, "取消排队中的 B 不该打断 A 正在跑的渲染"

    # Cancel A next — A is the one actually holding the semaphore right now.
    cancel_a = await client.post(f"/v1/predictions/{pid_a}/cancel", headers=headers_a)
    assert cancel_a.status_code == 200, cancel_a.text
    assert cancel_a.json()["status"] == "canceled"
    assert fc.interrupt_calls == 1, "取消真正持有信号量的 A 必须转发 interrupt"

    # Release A's render — this lets A's run_workflow_task honor the cancel it
    # already has (see existing single-task cancel-race test), and releases the
    # semaphore so B's queued invoke() finally proceeds: it must find its own
    # ExecutionTask already cancelled and raise *before* ever calling submit().
    render_gate.set()

    final_a = await _poll_prediction(client, pid_a, headers_a)
    assert final_a["status"] == "canceled", final_a

    final_b = await _poll_prediction(client, pid_b, headers_b)
    assert final_b["status"] == "canceled", final_b
    assert len(fc.submitted_graphs) == 1, "B 必须从未调用 submit()——只有 A 的图被提交过"


def test_task_to_dict_surfaces_video_thumbnails_and_url():
    """Task 7 Step 3:comfy 桥节点(comfy_bridge.py ComfyUIWorkflowNode)的产物形状是
    `thumbnails`(ffmpeg 首帧)/`video_url`(原片)/`items[].url`(全量),不是老的单一
    `image_url`。execution_task_serialize._image_urls 补齐这三种 key 前,
    这些 url 不进 output_thumbnails——不只是显示 bug:它们也不会被
    collect_referenced_image_uuids(同一 _image_urls)识别为"仍被引用",24h orphan
    reaper(main.py image_orphan_reap_loop)会把它们当无引用误删,即便任务历史仍在引用。
    """
    from datetime import datetime, timezone

    from src.api.routes.execution_tasks import _task_to_dict
    from src.models.execution_task import ExecutionTask

    now = datetime.now(timezone.utc)
    t = ExecutionTask(
        id=41, workflow_id=1, workflow_name="video-svc", status="completed",
        nodes_total=2, nodes_done=2, current_node=None,
        result={"outputs": {"out": {
            "items": [{"url": "/files/out.mp4?t=1", "kind": "video",
                       "filename": "out.mp4", "node_id": "92", "class_type": "SaveVideo"}],
            "video_url": "/files/out.mp4?t=1",
            "thumbnails": ["/files/thumb.png?t=1"],
            "seed": None,
        }}},
        error=None, duration_ms=100, created_at=now, updated_at=now,
    )
    d = _task_to_dict(t)
    assert "/files/thumb.png?t=1" in d["output_thumbnails"]
    assert "/files/out.mp4?t=1" in d["output_thumbnails"]
    # dedup:items[].url 和 video_url 是同一张(同一支视频原片),不该在 8 张缩略帽里占两格。
    assert d["output_thumbnails"].count("/files/out.mp4?t=1") == 1


class _AbortAwareFakeClient(FakeClient):
    """wait() 按生产语义轮询 `should_abort`:被取消就抛 `ComfyError` 退出。

    真 `ComfyClient.wait` 的实现细节(轮询频率、/queue 丢任务检测)由
    `tests/comfy/test_client.py` 单独覆盖;这里只验证桥节点**把探测接上去了**,
    以及取消抛错后整条链路的终态和信号量释放。
    """

    def __init__(self):
        super().__init__()
        self.abort_polls = 0

    async def wait(self, prompt_id, *, timeout_s, interval_s=2.0, should_abort=None, **_kw):
        assert should_abort is not None, "桥节点必须把取消探测传给 client.wait"
        while True:
            self.abort_polls += 1
            if await should_abort():
                raise ComfyError("渲染已取消")
            await asyncio.sleep(0.01)

    async def interrupt(self) -> None:
        pass  # sidecar 卡死时 /interrupt 无效 —— 本测试模拟的正是这种情况


async def _assert_semaphore_free(client, monkeypatch, name: str) -> None:
    """信号量真的被释放了:换一个正常的 FakeClient 再跑一单,必须能在轮询超时内跑完。

    (若前一单的节点还占着 `_SEM`,这一单会永远卡在 `async with _SEM:` 上,
    `_poll_prediction` 5s 超时 → 断言失败。)"""
    monkeypatch.setattr(nb, "get_client", lambda: FakeClient())
    raw_key = await _make_service_and_key(client, name)
    headers = {"Authorization": f"Bearer {raw_key}"}
    r = await client.post(
        f"/v1/services/{name}/predictions", json={"input": {"prompt": "hi"}},
        headers={**headers, "Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    final = await _poll_prediction(client, r.json()["id"], headers)
    assert final["status"] == "succeeded", final


@pytest.mark.asyncio
async def test_cancel_during_wait_exits_render_and_frees_semaphore(client, monkeypatch):
    """2026-09-03 事故的正面用例:任务卡在 ComfyUI 渲染里被取消。

    `/interrupt` 只在 ComfyUI 的节点边界生效 —— 卡在某节点内部(那次是一个进入
    CLOSE_WAIT 的 HF 下载)时它救不回来,旧代码的 `wait()` 会一路等到
    NOUS_COMFY_TIMEOUT(默认 4 小时)且全程占着 `_SEM`,后面所有 comfy 服务全堵死。
    现在 wait 自己按低频复查 DB 状态:落了 cancelled 就抛错退出。

    断言:① 终态仍是 canceled(不被 ComfyError 覆盖成 failed —— workflow_runner 的
    honor-cancelled 分支);② 信号量释放,后续任务能正常跑完。
    """
    fc = _AbortAwareFakeClient()
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    monkeypatch.setattr(comfy_templates_route, "get_client", lambda: fc)
    _patch_no_thumbnail(monkeypatch)

    raw_key = await _make_service_and_key(client, "nous-e2e-abort-wait")
    headers = {"Authorization": f"Bearer {raw_key}"}

    r = await client.post(
        "/v1/services/nous-e2e-abort-wait/predictions", json={"input": {"prompt": "hi"}},
        headers={**headers, "Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    pid = r.json()["id"]
    await _wait_running_task_id(int(pid))  # 节点已持有 _SEM、卡在 wait() 里

    cancel_resp = await client.post(f"/v1/predictions/{pid}/cancel", headers=headers)
    assert cancel_resp.status_code == 200, cancel_resp.text

    # 没有任何 gate 被放行 —— 节点退出完全靠 wait 内部的取消探测。先等它真的放手
    # (prediction 状态在 cancel 落库那一刻就是 canceled 了,单看状态证明不了节点已退出)。
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline and nb.get_running_task_id() is not None:
        await asyncio.sleep(0.005)
    assert nb.get_running_task_id() is None, "取消后桥节点没在 5s 内退出 wait 并释放信号量"
    assert fc.abort_polls >= 1

    final = await _poll_prediction(client, pid, headers)
    assert final["status"] == "canceled", final
    assert final["output"] is None, final

    await _assert_semaphore_free(client, monkeypatch, "nous-e2e-abort-wait-next")


@pytest.mark.asyncio
async def test_sidecar_lost_prompt_fails_task_and_frees_semaphore(client, monkeypatch):
    """sidecar 重启(或队列被清)→ prompt 既不在 /queue 也永不进 /history:
    `wait()` 判丢失抛错 → 任务落 failed(错误信息里有「丢失」),信号量释放。
    旧行为是干等到 4 小时超时,期间所有 comfy 服务排队堵死。"""
    class _LostFakeClient(FakeClient):
        async def wait(self, prompt_id, *, timeout_s, interval_s=2.0, **_kw):
            raise ComfyError("ComfyUI 侧任务已丢失(sidecar 可能重启过)")

    monkeypatch.setattr(nb, "get_client", lambda: _LostFakeClient())
    _patch_no_thumbnail(monkeypatch)

    raw_key = await _make_service_and_key(client, "nous-e2e-lost-prompt")
    headers = {"Authorization": f"Bearer {raw_key}"}

    r = await client.post(
        "/v1/services/nous-e2e-lost-prompt/predictions", json={"input": {"prompt": "hi"}},
        headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed", body
    assert "丢失" in (body["error"] or ""), body
    assert nb.get_running_task_id() is None

    await _assert_semaphore_free(client, monkeypatch, "nous-e2e-lost-prompt-next")
