"""ComfyUI sidecar 的健康体检 —— 不只是「端口有人应答」,还要认身份、核监听地址。

2026-09-25 事故:systemd 管的 nous-engine-comfyui 被别的进程 kill 掉,换成一个手工起的、
只绑 127.0.0.1 的实例。桥照样能打通(/queue 有应答),于是停了一天多没有任何地方显示
异常 —— Tailscale 那边打不开,systemd 也不再兜重启。所以这里三件事一起看:

  1. 在线:ComfyClient.health()(/queue 可达)
  2. 身份:占端口的 pid 是不是 systemd 单元的 MainPID(或其子进程)
  3. 监听:实际绑的地址 vs infra/network.env 的 NOUS_COMFY_LISTEN

外部命令(systemctl / ss)任何失败都降级成 None / 空,绝不抛 —— /health 与状态页靠它,
体检本身坏了不能拖垮它们。结果 5s 缓存(同 routes/_readiness 的 sidecar 在线探测)。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)

_CMD_TIMEOUT_S = 3.0
_CACHE_TTL_S = 5.0
_cache: tuple[float, dict] | None = None   # (monotonic 时刻, 结果)

# backend/src/services/comfy/sidecar_status.py → 仓库根
_NETWORK_ENV = Path(__file__).resolve().parents[4] / "infra" / "network.env"
_PID_RE = re.compile(r"pid=(\d+)")


def reset_cache() -> None:
    global _cache
    _cache = None


def _unit_name() -> str:
    return os.getenv("NOUS_COMFY_UNIT", "nous-engine-comfyui.service")


def _comfy_port() -> int:
    from src.services.comfy.client import _base_url  # noqa: PLC0415
    parsed = urllib.parse.urlparse(_base_url())
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def expected_listens(path: Path | None = None) -> list[str]:
    """network.env 里的 NOUS_COMFY_LISTEN;读不到 / 没这一行 → []。"""
    try:
        text = (path or _NETWORK_ENV).read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("NOUS_COMFY_LISTEN="):
            value = line.split("=", 1)[1].strip()
            return [a.strip() for a in value.split(",") if a.strip()]
    return []


async def _run_cmd(*argv: str) -> str | None:
    """跑一条外部命令,返回 stdout;非 0 退出 / 超时 / 找不到命令 → None。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_CMD_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace")


async def _probe_online() -> bool:
    from src.services.comfy import client as comfy_client  # noqa: PLC0415
    try:
        return bool((await comfy_client.get_comfy_client().health()).get("online"))
    except Exception:  # noqa: BLE001 — 探测失败一律按离线
        return False


def _parent_pid(pid: int) -> int | None:
    """/proc/<pid>/stat 的 ppid;读不到 → None。comm 可能含空格/括号,从最后一个 ')' 后切。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _descends_from(pid: int, ancestor: int, max_depth: int = 8) -> bool:
    """pid 就是 ancestor 或在其子孙里(ExecStart 若是包装脚本,监听的是子进程)。"""
    cur: int | None = pid
    for _ in range(max_depth):
        if cur is None or cur <= 1:
            return False
        if cur == ancestor:
            return True
        cur = _parent_pid(cur)
    return False


def _parse_unit(out: str | None) -> tuple[bool | None, int | None]:
    """`systemctl show -p ActiveState -p MainPID --value` → (active, main_pid)。

    --value 只输出值、不带属性名,且顺序由 systemd 决定而非 -p 的顺序 —— 所以按内容认:
    纯数字那行是 MainPID(0 = 没有主进程),另一行是 ActiveState。
    """
    if out is None:
        return None, None
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    main_pid = next((int(ln) for ln in lines if ln.isdigit()), None)
    states = [ln for ln in lines if not ln.isdigit()]
    if not states:
        return None, None
    return states[0] == "active", (main_pid or None)


def _parse_ss(out: str) -> tuple[list[str], set[int]]:
    """`ss -lntpH` 的行 → (监听地址列表, 占用 pid 集合)。地址去掉端口与 IPv6 方括号。"""
    listens: list[str] = []
    pids: set[int] = set()
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        addr = cols[3].rsplit(":", 1)[0].strip("[]")
        if addr and addr not in listens:
            listens.append(addr)
        pids.update(int(p) for p in _PID_RE.findall(line))
    return listens, pids


async def _compute() -> dict:
    port = _comfy_port()
    unit = _unit_name()
    unit_short = unit.removesuffix(".service")

    online, unit_out, ss_out = await asyncio.gather(
        _probe_online(),
        _run_cmd("systemctl", "show", unit, "-p", "ActiveState", "-p", "MainPID", "--value"),
        _run_cmd("ss", "-lntpH", f"sport = :{port}"),
    )
    unit_active, main_pid = _parse_unit(unit_out)

    problems: list[str] = []
    listens: list[str] = []
    missing: list[str] = []
    identity = "unknown"
    foreign_pids: list[int] = []

    if ss_out is not None:
        listens, pids = _parse_ss(ss_out)
        if not listens:
            identity = "none"
        elif pids and main_pid and unit_active:
            ours = {p for p in pids if _descends_from(p, main_pid)}
            identity = "systemd" if ours else "foreign"
            foreign_pids = sorted(pids - ours)
        elif pids:
            # 单元没在跑(或查不到 MainPID),端口却有人占 → 必然不是 systemd 那个
            identity = "foreign"
            foreign_pids = sorted(pids)
        else:
            # ss 看不到 pid(占用者是别的用户):只能按单元状态推断
            identity = "systemd" if unit_active else "foreign"
        # 只在 ss 真的查到了才比地址,否则会把全部期望地址误报成缺失
        missing = [a for a in expected_listens() if a not in listens]

    if not online:
        problems.append(f"ComfyUI 未响应(:{port})")
    if unit_active is False:
        problems.append(f"systemd 单元 {unit_short} 未运行")
    if identity == "foreign":
        who = f"(pid {', '.join(map(str, foreign_pids))})" if foreign_pids else ""
        problems.append(f"占用 {port} 的不是 systemd 管理的实例{who}")
    for addr in missing:
        hint = "(Tailscale 那边打不开)" if addr.startswith("100.") else ""
        problems.append(f"缺少监听地址 {addr}{hint}")

    # identity == "unknown"(ss 不可用)不降级:体检工具坏了不等于 ComfyUI 坏了
    if not online:
        state = "down"
    elif identity in ("foreign", "none") or missing or unit_active is False:
        state = "degraded"
    else:
        state = "ok"

    return {
        "state": state,
        "online": online,
        "unit_active": unit_active,
        "identity": identity,
        "listens": listens,
        "missing_listens": missing,
        "problems": problems,
        "checked_at": time.time(),
    }


async def sidecar_status() -> dict:
    """ComfyUI sidecar 体检结果(5s 缓存)。字段见模块 docstring;绝不抛。

    返回 `{state: ok|degraded|down, online, unit_active: bool|None,
    identity: systemd|foreign|none|unknown, listens, missing_listens, problems, checked_at}`。
    """
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_S:
        return _cache[1]
    try:
        result = await _compute()
    except Exception as e:  # noqa: BLE001 — 体检自身出错按 down 报,不让调用方 500
        logger.warning("comfy sidecar status failed: %s", e)
        result = {
            "state": "down", "online": False, "unit_active": None, "identity": "unknown",
            "listens": [], "missing_listens": [], "problems": [f"ComfyUI 体检失败:{e}"],
            "checked_at": time.time(),
        }
    _cache = (now, result)
    return result
