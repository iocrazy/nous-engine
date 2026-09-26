"""ComfyUI sidecar 体检(comfy/sidecar_status.py)单测 —— 全部打桩,不碰真 systemd / ss / ComfyUI。

覆盖 2026-09-25 事故的四种形态:ok / 端口被外来进程占(foreign)/ 缺监听地址 / 离线;
外部命令失败降级不抛;5s 缓存。
"""
from __future__ import annotations

import pytest

from src.services.comfy import sidecar_status as cs

# conftest 的 autouse 桩默认把 sidecar_status 换成 ok;本模块要测真实现。
REAL_COMFY_SIDECAR_STATUS = True

EXPECTED = ["127.0.0.1", "127.0.1.1", "100.124.149.118"]
MAIN_PID = 3125684


def _ss_line(addr: str, pid: int) -> str:
    return f'LISTEN 0      128    {addr}:8888 0.0.0.0:* users:(("python",pid={pid},fd=77))'


def _install(monkeypatch, *, online=True, unit="active\n3125684\n", ss=None, calls=None):
    """打桩:在线探测、两条外部命令、期望地址表。unit/ss 传 None = 命令失败。"""
    monkeypatch.setenv("NOUS_COMFY_URL", "http://127.0.0.1:8888")
    monkeypatch.setattr(cs, "expected_listens", lambda path=None: list(EXPECTED))

    async def probe():
        if calls is not None:
            calls.append("probe")
        return online
    monkeypatch.setattr(cs, "_probe_online", probe)

    async def run(*argv):
        if argv[0] == "systemctl":
            assert "nous-engine-comfyui.service" in argv
            return unit
        if argv[0] == "ss":
            assert argv[-1] == "sport = :8888"
            return ss
        raise AssertionError(argv)
    monkeypatch.setattr(cs, "_run_cmd", run)
    # 默认没有进程树关系(foreign 用例里的 pid 不是 MainPID 的子孙)
    monkeypatch.setattr(cs, "_parent_pid", lambda pid: None)


@pytest.mark.asyncio
async def test_ok_when_systemd_instance_binds_all(monkeypatch):
    _install(monkeypatch, ss="\n".join(_ss_line(a, MAIN_PID) for a in EXPECTED))
    r = await cs.sidecar_status()
    assert r["state"] == "ok"
    assert r["online"] is True and r["unit_active"] is True
    assert r["identity"] == "systemd"
    assert r["listens"] == EXPECTED
    assert r["missing_listens"] == [] and r["problems"] == []


@pytest.mark.asyncio
async def test_child_of_main_pid_counts_as_systemd(monkeypatch):
    """ExecStart 若是包装脚本,监听的是 MainPID 的子进程,仍算 systemd 身份。"""
    _install(monkeypatch, ss="\n".join(_ss_line(a, 777) for a in EXPECTED))
    monkeypatch.setattr(cs, "_parent_pid", lambda pid: MAIN_PID if pid == 777 else 1)
    r = await cs.sidecar_status()
    assert r["identity"] == "systemd" and r["state"] == "ok"


@pytest.mark.asyncio
async def test_foreign_instance_is_degraded(monkeypatch):
    """事故原形:单元 inactive,8888 被手工起的 python 占着、只绑 127.0.0.1。"""
    _install(monkeypatch, unit="inactive\n0\n", ss=_ss_line("127.0.0.1", 4242))
    r = await cs.sidecar_status()
    assert r["state"] == "degraded"
    assert r["online"] is True
    assert r["unit_active"] is False
    assert r["identity"] == "foreign"
    assert r["listens"] == ["127.0.0.1"]
    assert r["missing_listens"] == ["127.0.1.1", "100.124.149.118"]
    assert "systemd 单元 nous-engine-comfyui 未运行" in r["problems"]
    assert "占用 8888 的不是 systemd 管理的实例(pid 4242)" in r["problems"]
    assert "缺少监听地址 100.124.149.118(Tailscale 那边打不开)" in r["problems"]


@pytest.mark.asyncio
async def test_foreign_pid_while_unit_active(monkeypatch):
    """单元 active,但端口被另一个 pid 占(systemd 那个没抢到端口)。"""
    _install(monkeypatch, ss="\n".join(_ss_line(a, 999) for a in EXPECTED))
    r = await cs.sidecar_status()
    assert r["identity"] == "foreign" and r["state"] == "degraded"
    assert any("pid 999" in p for p in r["problems"])


@pytest.mark.asyncio
async def test_missing_listen_is_degraded(monkeypatch):
    _install(monkeypatch, ss="\n".join(_ss_line(a, MAIN_PID) for a in EXPECTED[:2]))
    r = await cs.sidecar_status()
    assert r["state"] == "degraded"
    assert r["identity"] == "systemd"
    assert r["missing_listens"] == ["100.124.149.118"]
    assert r["problems"] == ["缺少监听地址 100.124.149.118(Tailscale 那边打不开)"]


@pytest.mark.asyncio
async def test_down_when_offline(monkeypatch):
    _install(monkeypatch, online=False, unit="failed\n0\n", ss="")
    r = await cs.sidecar_status()
    assert r["state"] == "down"
    assert r["identity"] == "none"
    assert r["problems"][0] == "ComfyUI 未响应(:8888)"
    assert "systemd 单元 nous-engine-comfyui 未运行" in r["problems"]


@pytest.mark.asyncio
async def test_command_failures_degrade_to_unknown_not_raise(monkeypatch):
    """systemctl / ss 都跑不了:不抛,unit_active=None,不误报缺地址,不因工具坏而降级。"""
    _install(monkeypatch, unit=None, ss=None)
    r = await cs.sidecar_status()
    assert r["unit_active"] is None
    assert r["identity"] == "unknown"
    assert r["listens"] == [] and r["missing_listens"] == []
    assert r["state"] == "ok" and r["problems"] == []


@pytest.mark.asyncio
async def test_internal_exception_reported_as_down(monkeypatch):
    async def boom():
        raise RuntimeError("kaboom")
    monkeypatch.setattr(cs, "_compute", boom)
    r = await cs.sidecar_status()
    assert r["state"] == "down" and r["online"] is False
    assert "kaboom" in r["problems"][0]


@pytest.mark.asyncio
async def test_result_cached_for_ttl(monkeypatch):
    calls: list[str] = []
    _install(monkeypatch, ss=_ss_line("127.0.0.1", MAIN_PID), calls=calls)
    first = await cs.sidecar_status()
    second = await cs.sidecar_status()
    assert calls == ["probe"] and first is second
    cs.reset_cache()
    await cs.sidecar_status()
    assert calls == ["probe", "probe"]


@pytest.mark.asyncio
async def test_run_cmd_missing_binary_returns_none():
    assert await cs._run_cmd("definitely-not-a-real-binary-xyz") is None


@pytest.mark.asyncio
async def test_run_cmd_nonzero_exit_returns_none():
    assert await cs._run_cmd("false") is None


def test_expected_listens_parses_network_env(tmp_path):
    env = tmp_path / "network.env"
    env.write_text("# c\nNOUS_TS_HOST=1.2.3.4\nNOUS_COMFY_LISTEN=127.0.0.1, 1.2.3.4\n")
    assert cs.expected_listens(env) == ["127.0.0.1", "1.2.3.4"]
    assert cs.expected_listens(tmp_path / "missing.env") == []


def test_default_network_env_path_points_at_repo():
    """路径按仓库根推;真文件里的地址表必须能读出来且含回环(桥靠它)。"""
    assert "127.0.0.1" in cs.expected_listens()


def test_parse_ss_ipv6_brackets():
    listens, pids = cs._parse_ss('LISTEN 0 128 [::1]:8888 [::]:* users:(("python",pid=5,fd=3))')
    assert listens == ["::1"] and pids == {5}
