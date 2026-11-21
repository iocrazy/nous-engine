import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import httpx
from src.api import admin_session
import src.api.routes.comfy_templates as ct_mod
from src.services.nodes import comfy_bridge


DEVICE = {
    "name": "cuda:0 NVIDIA RTX PRO 6000 Blackwell Workstation Edition : cudaMallocAsync",
    "type": "cuda",
    "index": 0,
    "vram_total": 102_000_000_000,
    "vram_free": 37_700_000_000,
    "torch_vram_total": 60_000_000_000,
}


class FakeClient:
    async def health(self):
        return {"online": True, "queue_depth": 2, "version": "0.30.2"}
    async def object_info(self):
        return {"RandomNoise": {"input": {"required": {"noise_seed": ["INT", {"default": 0}]}}}}
    async def system_stats(self):
        return {"devices": [DEVICE]}
    async def free(self):
        return None


@pytest.mark.asyncio
async def test_health(client, monkeypatch):
    monkeypatch.setattr(ct_mod, "get_client", lambda: FakeClient())
    r = await client.get("/api/v1/comfy/health")
    assert r.status_code == 200 and r.json()["queue_depth"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("authenticated", "action", "depth", "running", "exit_code", "expected"), [
    (False, True, 0, False, 0, 401),
    (True, False, 0, False, 0, 400),
    (True, True, 2, False, 0, 409),
    (True, True, 0, True, 0, 409),
    (True, True, 0, False, 1, 503),
    (True, True, 0, False, 0, 202),
])
async def test_comfy_restart(client, monkeypatch, tmp_path, authenticated, action, depth, running, exit_code, expected):
    monkeypatch.setattr(admin_session, "get_settings", lambda: SimpleNamespace(
        ADMIN_PASSWORD="", ADMIN_TOKEN="test-admin", ADMIN_SESSION_SECRET="",
    ))

    class Sidecar(FakeClient):
        async def health(self):
            return {"online": bool(depth), "queue_depth": depth}

    monkeypatch.setattr(ct_mod, "get_client", Sidecar)
    monkeypatch.setattr(comfy_bridge, "get_running_render", lambda: {"task_id": 1} if running else None)
    command = ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "--no-block", "restart", "nous-engine-comfyui.service"]
    run = Mock(side_effect=subprocess.CalledProcessError(exit_code, command) if exit_code else None)
    monkeypatch.setattr(ct_mod, "subprocess", SimpleNamespace(
        run=run, DEVNULL=subprocess.DEVNULL, SubprocessError=subprocess.SubprocessError,
        CalledProcessError=subprocess.CalledProcessError,
    ))
    monkeypatch.setattr(ct_mod, "_COMFY_RESTART_RULE", tmp_path / "nous-engine-comfy-restart")
    headers = {"Authorization": "Bearer test-admin"} if authenticated else {}
    if action:
        headers["X-Nous-Action"] = "restart-comfyui"
    response = await client.post("/api/v1/comfy/restart", headers=headers)
    assert response.status_code == expected, response.text
    if expected in (202, 503):
        run.assert_called_once_with(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=True)
    else:
        run.assert_not_called()
    if expected == 202:
        assert response.json() == {"accepted": True}
    if expected == 503:
        assert "尚未安装 ComfyUI 重启授权" in response.text


@pytest.mark.asyncio
async def test_health_includes_devices_with_computed_vram_used(client, monkeypatch):
    monkeypatch.setattr(ct_mod, "get_client", lambda: FakeClient())
    r = await client.get("/api/v1/comfy/health")
    body = r.json()
    assert body["devices"] == [{
        "name": DEVICE["name"],
        "type": "cuda",
        "index": 0,
        "vram_total": 102_000_000_000,
        "vram_free": 37_700_000_000,
        "vram_used": 102_000_000_000 - 37_700_000_000,
        "comfy_used": 60_000_000_000,
        "torch_vram_total": 60_000_000_000,
    }]


@pytest.mark.asyncio
async def test_health_offline_devices_empty(client, monkeypatch):
    class OfflineClient(FakeClient):
        async def health(self):
            return {"online": False, "queue_depth": 0, "version": ""}
    monkeypatch.setattr(ct_mod, "get_client", lambda: OfflineClient())
    r = await client.get("/api/v1/comfy/health")
    assert r.status_code == 200
    assert r.json()["devices"] == []


@pytest.mark.asyncio
async def test_free_calls_client_and_returns_snapshot(client, monkeypatch):
    calls = {"n": 0}
    class Counting(FakeClient):
        async def free(self):
            calls["n"] += 1
    monkeypatch.setattr(ct_mod, "get_client", lambda: Counting())
    monkeypatch.setattr(ct_mod, "_FREE_POLL_INTERVAL", 0)  # 显存不降时别空等 6s
    r = await client.post("/api/v1/comfy/free")
    assert r.status_code == 200
    assert calls["n"] == 1
    body = r.json()
    assert body["ok"] is True
    assert body["devices"][0]["vram_used"] == 102_000_000_000 - 37_700_000_000
    # 显存没降 → settled False,前端据此提示"释放已触发,可能仍在进行"
    assert body["settled"] is False


@pytest.mark.asyncio
async def test_free_waits_for_async_unload_to_land(client, monkeypatch):
    """ComfyUI 的 /free 只设 flag,真卸载晚几秒才发生(实测 64.3G→43.8G)。
    路由要轮询到显存真降下来再返回,否则按钮看着像没生效。"""
    state = {"round": 0}

    class Lagging(FakeClient):
        async def free(self):
            return None

        async def system_stats(self):
            # 第 3 次查询才反映卸载完成(模拟 worker 被唤醒后的延迟)。降的是
            # **torch_vram_total**(ComfyUI 自占)——判定只认这个,不认整卡读数,
            # 否则同卡 vLLM 的加载/卸载会被误判成"释放成功/失败"。
            state["round"] += 1
            torch_b = 25_000_000_000 if state["round"] < 3 else 200_000_000
            return {"devices": [{"name": "cuda:0 Test GPU", "type": "cuda", "index": 0,
                                 "vram_total": 102_000_000_000, "vram_free": 37_700_000_000,
                                 "torch_vram_total": torch_b}]}

    monkeypatch.setattr(ct_mod, "get_client", lambda: Lagging())
    monkeypatch.setattr(ct_mod, "_FREE_POLL_INTERVAL", 0)
    body = (await client.post("/api/v1/comfy/free")).json()
    assert body["settled"] is True
    assert body["freed_bytes"] > 1_000_000_000
    assert body["devices"][0]["comfy_used"] == 200_000_000


@pytest.mark.asyncio
async def test_free_propagates_comfy_error(client, monkeypatch):
    from src.services.comfy.client import ComfyError

    class FailingClient(FakeClient):
        async def free(self):
            raise ComfyError("释放显存失败(HTTP 500)")
    monkeypatch.setattr(ct_mod, "get_client", lambda: FailingClient())
    r = await client.post("/api/v1/comfy/free")
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_health_reports_comfy_main_process_memory(client, monkeypatch):
    from src.services.comfy import sidecar_status

    async def local_sidecar():
        return {"main_pid": 1234}

    def rss(pid):
        assert pid == 1234
        return 4 * 1024 ** 3

    monkeypatch.setattr(sidecar_status, "sidecar_status", local_sidecar)
    monkeypatch.setattr(ct_mod, "get_client", lambda: FakeClient())
    monkeypatch.setattr(ct_mod, "_process_rss_bytes", rss)
    response = await client.get("/api/v1/comfy/health")
    assert response.json()["process_rss_bytes"] == 4 * 1024 ** 3


@pytest.mark.asyncio
@pytest.mark.parametrize(("online", "depth", "render", "status"), [
    (True, 0, None, 200),
    (True, 1, None, 409),
    (True, 0, {"task_id": "123", "held_seconds": 4}, 409),
    (False, 0, None, 503),
])
async def test_idle_only_free_checks_queue_and_active_render(client, monkeypatch, online, depth, render, status):
    from src.services.nodes import comfy_bridge

    released = []

    class Sidecar(FakeClient):
        async def health(self):
            return {"online": online, "queue_depth": depth}

        async def free(self):
            released.append(True)

    monkeypatch.setattr(ct_mod, "get_client", Sidecar)
    monkeypatch.setattr(ct_mod, "_FREE_POLL_INTERVAL", 0)
    monkeypatch.setattr(comfy_bridge, "get_running_render", lambda: render)

    response = await client.post("/api/v1/comfy/free?idle_only=true")
    assert response.status_code == status
    assert bool(released) == (status == 200)


@pytest.mark.asyncio
async def test_object_info_cached(client, monkeypatch):
    calls = {"n": 0}
    class Counting(FakeClient):
        async def object_info(self):
            calls["n"] += 1
            return await super().object_info()
    monkeypatch.setattr(ct_mod, "get_client", lambda: Counting())
    ct_mod._object_info_cache = None
    await client.get("/api/v1/comfy/object-info")
    await client.get("/api/v1/comfy/object-info")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_object_info_sidecar_unreachable(client, monkeypatch):
    class FailingClient(FakeClient):
        async def object_info(self):
            raise httpx.ConnectError("sidecar down")
    monkeypatch.setattr(ct_mod, "get_client", lambda: FailingClient())
    ct_mod._object_info_cache = None
    r = await client.get("/api/v1/comfy/object-info")
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_free_noop_when_nothing_resident(client, monkeypatch):
    """空载时(只剩 ~1.6G torch 上下文)点释放:落定且归还 0,UI 说"没有可释放的",
    不能报"仍在卸载"误导用户(实机验证发现)。"""
    class Idle(FakeClient):
        async def free(self):
            return None

        async def system_stats(self):
            # 整卡被别的进程占着(43.8G),但 ComfyUI 自占只剩 0.1G → 无可释放
            return {"devices": [{"name": "cuda:0 Test GPU", "type": "cuda", "index": 0,
                                 "vram_total": 102_000_000_000,
                                 "vram_free": 58_200_000_000, "torch_vram_total": 100_000_000}]}

    monkeypatch.setattr(ct_mod, "get_client", lambda: Idle())
    monkeypatch.setattr(ct_mod, "_FREE_POLL_INTERVAL", 0)
    body = (await client.post("/api/v1/comfy/free")).json()
    assert body["settled"] is True and body["freed_bytes"] == 0


@pytest.mark.asyncio
async def test_health_reports_running_render(client, monkeypatch):
    """排障字段:谁占着渲染信号量、占了多久。

    2026-09-03 事故里"所有 comfy 服务都不动了"要翻日志才知道是哪个任务卡着、卡了多久
    —— 现在健康面板直接给 `{task_id, held_seconds}`(空闲 None)。
    """
    import time

    import src.services.nodes.comfy_bridge as nb

    monkeypatch.setattr(ct_mod, "get_client", lambda: FakeClient())

    r = await client.get("/api/v1/comfy/health")
    assert r.status_code == 200
    assert r.json()["running_render"] is None  # 空闲

    monkeypatch.setattr(nb, "_running_task_id", 4242)
    monkeypatch.setattr(nb, "_running_since", time.monotonic() - 35 * 60)
    body = (await client.get("/api/v1/comfy/health")).json()["running_render"]
    assert body["task_id"] == 4242
    assert body["held_seconds"] >= 35 * 60
