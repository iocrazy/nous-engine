# 进程表网络流量列 + 可排序 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 管理台「系统状态 → 进程」表加一列每进程网络收发速率(覆盖所有进程含 docker/root),CPU% / MEM / 网络三列可点击排序。

**Architecture:** 一个 root 的 systemd 单元 `nous-engine-netprobe` 跑 bpftrace(kretprobe 四组 TCP/UDP 收发函数按 pid 累加字节),经纯 stdlib 包装器每 2s 原子写 `/run/nous-engine/net_by_pid.json`;后端 `monitor.py` 读该文件并入现有 `_top_processes` 输出(`net_tx_bps`/`net_rx_bps`,不可用为 `null`)并在 `/api/v1/monitor/stats` 加 `net_probe`;前端进程表加排序状态与「网络」列。后端不提权。

**Tech Stack:** bpftrace 0.25(内核 7.0)、systemd、Python 3.12 stdlib(包装器)/ psutil + FastAPI(后端)、React + vitest(前端)、bash(install.sh)。

**Spec:** `docs/superpowers/specs/2026-09-06-process-net-traffic-design.md`

## Global Constraints

- 每个 Task 独立分支 + PR(`git -c commit.gpgsign=false commit`);CI 绿后 squash 合并;合并后工作树切回 master。**不用 worktree**(生产 venv 不可搬迁)。
- 后端测试只跑 PostgreSQL;全量与 CI 对齐:`cd backend && uv run pytest tests -n 8 -q --ignore=tests/test_integration_smoke.py --ignore=tests/test_tts_engine_adapter.py`。绝不裸 `uv sync`。
- 采集器**只累加字节数**,不记录地址/端口/内容;文件只有 `pid → {tx, rx}`(spec §4)。
- 后端进程 `heygo` 不提权;采集器是独立 root 单元(spec §2.1)。
- 文件新鲜阈值 **10s**;不可用时 `net_tx_bps`/`net_rx_bps` 为 `null`,`net_probe.available=false`(spec §2.2)。
- 进程列表 = CPU 前 **20** ∪ 有流量的 pid,**总上限 40**(spec §2.2)。
- 前端默认排序 `cpu desc`,再点同列切升降;排序后显示前 **15** 行(spec §2.3)。
- 文件路径常量:`NOUS_NET_BY_PID` 环境变量覆盖,默认 `/run/nous-engine/net_by_pid.json`。
- 不做:历史曲线 / 按连接或域名拆分 / 告警 / 磁盘 I/O 列(spec §6)。
- 真机验证要 root 的步骤(`bpftrace --dry-run`、`install.sh`)由用户在自己终端敲,实施者只准备命令并在报告里列出。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `infra/monitoring/netprobe.bt`(新) | bpftrace 脚本:4 组 kretprobe 按 pid 累加 tx/rx,每 2s 打印并清零 |
| `infra/monitoring/nous-netprobe.py`(新) | 包装器:起 bpftrace `-f json`,把每个 interval 的 `@tx`/`@rx` 合成 JSON 原子写文件;纯 stdlib |
| `infra/systemd/nous-engine-netprobe.service`(新) | root 单元,`RuntimeDirectory=nous-engine`,Restart=on-failure |
| `backend/tests/test_netprobe_wrapper.py`(新) | 包装器纯函数测试(解析 bpftrace JSON 行、合成、原子写) |
| `backend/src/api/routes/monitor.py`(改) | `_read_net_by_pid`、`_top_processes(net=)`、响应加 `net_probe` |
| `backend/tests/test_monitor_net_probe.py`(新) | 后端读文件/新鲜度/并入/补 pid 测试 |
| `frontend/src/components/overlays/processSort.ts`(新) | `sortProcesses` / `formatRate` 纯函数 |
| `frontend/src/components/overlays/processSort.test.ts`(新) | vitest |
| `frontend/src/api/system.ts`(改) | `ProcessInfo` 加两字段;`MonitorStatsResponse` 加 `net_probe`;`useSysProcesses` 一并返回 `net_probe` |
| `frontend/src/components/overlays/DashboardOverlay.tsx`(改) | 表头可点排序、「网络」列 |
| `infra/systemd/install.sh`、`infra/systemd/nous-engine.target`、`infra/systemd/enginectl`、`infra/systemd/README.md`(改) | 装采集器(有 bpftrace 才装)、dry-run 自检、纳入 target/enginectl、清 healthprobe drop-in |

---

### Task 1: 采集器 — bpftrace 脚本 + stdlib 包装器 + root 单元

**Files:**
- Create: `infra/monitoring/netprobe.bt`
- Create: `infra/monitoring/nous-netprobe.py`
- Create: `infra/systemd/nous-engine-netprobe.service`
- Test: `backend/tests/test_netprobe_wrapper.py`

**Interfaces:**
- Consumes: 无。
- Produces: 文件 `/run/nous-engine/net_by_pid.json`,内容
  `{"ts": <float epoch>, "interval_s": 2, "pids": {"<pid>": {"tx": <int bytes>, "rx": <int bytes>}}}`
  (Task 2 按此读);包装器纯函数 `parse_line(line: str) -> tuple[str, dict[int, int]] | None`、
  `build_payload(tx: dict[int,int], rx: dict[int,int], interval_s: int, now: float) -> dict`、
  `atomic_write_json(path: str, payload: dict) -> None`。

- [ ] **Step 1: 写包装器的失败测试**

`backend/tests/test_netprobe_wrapper.py`:

```python
"""infra/monitoring/nous-netprobe.py 的纯函数测试(spec 2026-09-06 process-net-traffic §2.1)。
包装器是系统 python3 跑的 stdlib 脚本,不在 src 包里 —— 按路径 import。"""
import importlib.util
import json
import os
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("nous_netprobe", _REPO / "infra/monitoring/nous-netprobe.py")
netprobe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(netprobe)


def test_parse_line_map_tx():
    line = json.dumps({"type": "map", "data": {"@tx": {"1234": 100, "77": 5}}})
    assert netprobe.parse_line(line) == ("tx", {1234: 100, 77: 5})


def test_parse_line_map_rx():
    line = json.dumps({"type": "map", "data": {"@rx": {"1234": 9}}})
    assert netprobe.parse_line(line) == ("rx", {1234: 9})


def test_parse_line_ignores_non_map_and_garbage():
    assert netprobe.parse_line(json.dumps({"type": "attached_probes", "data": {"probes": 8}})) is None
    assert netprobe.parse_line("not json") is None
    assert netprobe.parse_line("") is None


def test_build_payload_merges_tx_rx_and_keeps_zero_side():
    p = netprobe.build_payload({1: 10, 2: 20}, {2: 5, 3: 7}, interval_s=2, now=1000.5)
    assert p == {
        "ts": 1000.5,
        "interval_s": 2,
        "pids": {"1": {"tx": 10, "rx": 0}, "2": {"tx": 20, "rx": 5}, "3": {"tx": 0, "rx": 7}},
    }


def test_atomic_write_json_replaces_whole_file(tmp_path):
    target = tmp_path / "net_by_pid.json"
    target.write_text("{\"stale\": true}")
    netprobe.atomic_write_json(str(target), {"ts": 1.0, "interval_s": 2, "pids": {}})
    assert json.loads(target.read_text()) == {"ts": 1.0, "interval_s": 2, "pids": {}}
    # 不留临时文件
    assert sorted(os.listdir(tmp_path)) == ["net_by_pid.json"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_netprobe_wrapper.py -q`
Expected: FAIL(`FileNotFoundError` / 找不到 `infra/monitoring/nous-netprobe.py`)。

- [ ] **Step 3: 写 bpftrace 脚本**

`infra/monitoring/netprobe.bt`:

```
// nous-engine-netprobe:每进程网络收发字节(spec 2026-09-06 process-net-traffic §2.1)。
// 只累加字节数,不看地址/端口/内容。用 kretprobe 的返回值(实际收发字节)而不是入参的
// 请求长度:部分发送/接收时入参会高估。tcp_sendmsg/tcp_recvmsg/udp_sendmsg/udp_recvmsg
// 及 v6 版都是导出的稳定内核函数,比 tcp_cleanup_rbuf 之类内部符号更不容易随内核变。
// pid 在 bpftrace 里就是 tgid(进程),容器进程在宿主内核也是宿主 pid → docker 天然覆盖。
kretprobe:tcp_sendmsg, kretprobe:udp_sendmsg, kretprobe:udpv6_sendmsg
{
  if (retval > 0) { @tx[pid] += retval; }
}

kretprobe:tcp_recvmsg, kretprobe:udp_recvmsg, kretprobe:udpv6_recvmsg
{
  if (retval > 0) { @rx[pid] += retval; }
}

// 每 2s 吐一次并清零:包装器把同一轮的 @tx / @rx 合成一份 JSON。先 tx 后 rx,
// 包装器以收到 @rx 作为"这一轮齐了"的信号。
interval:s:2
{
  print(@tx);
  print(@rx);
  clear(@tx);
  clear(@rx);
}

END
{
  clear(@tx);
  clear(@rx);
}
```

- [ ] **Step 4: 写包装器**

`infra/monitoring/nous-netprobe.py`:

```python
#!/usr/bin/env python3
"""nous-engine-netprobe 包装器(spec 2026-09-06 process-net-traffic §2.1)。

起 `bpftrace -f json netprobe.bt`,逐行读它的 JSON 输出;每轮 interval 先来 @tx 再来 @rx,
收到 @rx 就把两张表合成一份 {"ts","interval_s","pids"} 原子写到 NOUS_NET_BY_PID
(默认 /run/nous-engine/net_by_pid.json)。后端(heygo)只读这个文件,本进程是 root 单元。
纯 stdlib,系统 /usr/bin/python3 跑。bpftrace 退出 → 本进程以其退出码退出 → systemd 拉起。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

INTERVAL_S = 2
DEFAULT_PATH = "/run/nous-engine/net_by_pid.json"
_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(_HERE, "netprobe.bt")


def parse_line(line: str) -> tuple[str, dict[int, int]] | None:
    """bpftrace -f json 的一行 → ("tx"|"rx", {pid: bytes});其它行/坏行 → None。"""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "map":
        return None
    data = obj.get("data") or {}
    for key, name in (("@tx", "tx"), ("@rx", "rx")):
        if key in data:
            raw = data[key] or {}
            out: dict[int, int] = {}
            for k, v in raw.items():
                try:
                    out[int(k)] = int(v)
                except (TypeError, ValueError):
                    continue
            return name, out
    return None


def build_payload(tx: dict[int, int], rx: dict[int, int], interval_s: int, now: float) -> dict:
    pids: dict[str, dict[str, int]] = {}
    for pid in sorted(set(tx) | set(rx)):
        pids[str(pid)] = {"tx": int(tx.get(pid, 0)), "rx": int(rx.get(pid, 0))}
    return {"ts": now, "interval_s": interval_s, "pids": pids}


def atomic_write_json(path: str, payload: dict) -> None:
    """写临时文件再 rename —— 读方永远看到完整 JSON,不会读到半截。"""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".net_by_pid.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.chmod(tmp, 0o644)  # 后端(非 root)要能读
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    path = os.environ.get("NOUS_NET_BY_PID", DEFAULT_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ["bpftrace", "-f", "json", SCRIPT]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=sys.stderr, text=True, bufsize=1)
    tx: dict[int, int] = {}
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            parsed = parse_line(line)
            if parsed is None:
                continue
            kind, table = parsed
            if kind == "tx":
                tx = table
                continue
            # kind == "rx":这一轮齐了
            atomic_write_json(path, build_payload(tx, table, INTERVAL_S, time.time()))
            tx = {}
    except KeyboardInterrupt:
        proc.terminate()
    rc = proc.wait()
    print(f"[nous-netprobe] bpftrace exited rc={rc}", file=sys.stderr)
    return rc if rc != 0 else 1  # bpftrace 不该自己退出;退了就让 systemd 重启


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_netprobe_wrapper.py -q`
Expected: 5 passed。

- [ ] **Step 6: 写 systemd 单元**

`infra/systemd/nous-engine-netprobe.service`:

```ini
[Unit]
Description=nous-engine 每进程网络流量采集(bpftrace,root;spec 2026-09-06 process-net-traffic)
# 只写 /run/nous-engine/net_by_pid.json 给后端读;后端(heygo)不提权。独立于后端存活。
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
# /run/nous-engine 由 systemd 建(0755),后端只读。
RuntimeDirectory=nous-engine
RuntimeDirectoryMode=0755
Environment=NOUS_NET_BY_PID=/run/nous-engine/net_by_pid.json
ExecStart=/usr/bin/python3 /media/heygo/program/projects-code/repos/nous-engine/infra/monitoring/nous-netprobe.py
# bpftrace 要 tracefs/bpf,除此之外锁死文件系统。
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/run/nous-engine
PrivateTmp=yes
MemoryMax=512M
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 7: 本地能做的静态检查**

Run: `python3 -m py_compile infra/monitoring/nous-netprobe.py && bash -c 'systemd-analyze verify infra/systemd/nous-engine-netprobe.service 2>&1 | grep -v "Unit .* is not loaded" || true'`
Expected: py_compile 无输出;verify 无 error 行(warning 可忽略)。
真机附着检查要 root,**实施者不做**,在报告里写明命令:`sudo bpftrace --dry-run infra/monitoring/netprobe.bt`(期望输出 `Attached 6 probes` 后退出 0)。

- [ ] **Step 8: Commit + PR**

```bash
git checkout -b feat/netprobe-collector master
git add infra/monitoring/netprobe.bt infra/monitoring/nous-netprobe.py infra/systemd/nous-engine-netprobe.service backend/tests/test_netprobe_wrapper.py
git -c commit.gpgsign=false commit -m "feat(infra): nous-engine-netprobe —— root bpftrace 采集每进程网络收发字节写 /run/nous-engine/net_by_pid.json"
git push -u origin feat/netprobe-collector && gh pr create --base master --fill
```

---

### Task 2: 后端 — 读采集文件并入进程表,响应加 `net_probe`

**Files:**
- Modify: `backend/src/api/routes/monitor.py`(`_top_processes` 约 195-214 行;`_compute_system_stats` 约 371-373、408 行)
- Test: `backend/tests/test_monitor_net_probe.py`

**Interfaces:**
- Consumes: Task 1 的文件格式 `{"ts", "interval_s", "pids": {"<pid>": {"tx","rx"}}}`。
- Produces:
  - `_read_net_by_pid(path: str | None = None, max_age_s: float = 10.0, now: float | None = None) -> tuple[dict[int, tuple[int, int]], dict]`
    → `({pid: (tx_bps, rx_bps)}, {"available": bool, "age_s": float | None})`
  - `_top_processes(limit: int = 20, net: dict[int, tuple[int, int]] | None = None, max_total: int = 40) -> list[dict]`,每行多两键 `net_tx_bps: int | None`、`net_rx_bps: int | None`
  - `/api/v1/monitor/stats` 顶层新键 `net_probe: {"available": bool, "age_s": float | None}`(Task 3 读)。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_monitor_net_probe.py`:

```python
"""monitor.py 读 netprobe 文件并入进程表(spec 2026-09-06 process-net-traffic §2.2)。"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest

from src.api.routes import monitor


def _write(tmp_path, ts, pids, interval_s=2):
    p = tmp_path / "net_by_pid.json"
    p.write_text(json.dumps({"ts": ts, "interval_s": interval_s, "pids": pids}))
    return str(p)


def test_read_fresh_file_gives_bps_and_available(tmp_path):
    path = _write(tmp_path, ts=1000.0, pids={"10": {"tx": 2000, "rx": 400}})
    net, probe = monitor._read_net_by_pid(path=path, now=1003.0)
    assert net == {10: (1000, 200)}
    assert probe == {"available": True, "age_s": 3.0}


def test_read_stale_file_is_unavailable(tmp_path):
    path = _write(tmp_path, ts=1000.0, pids={"10": {"tx": 2000, "rx": 400}})
    net, probe = monitor._read_net_by_pid(path=path, now=1030.0)
    assert net == {}
    assert probe == {"available": False, "age_s": 30.0}


def test_read_missing_or_garbage_is_unavailable(tmp_path):
    net, probe = monitor._read_net_by_pid(path=str(tmp_path / "nope.json"), now=1.0)
    assert (net, probe) == ({}, {"available": False, "age_s": None})
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    net, probe = monitor._read_net_by_pid(path=str(bad), now=1.0)
    assert (net, probe) == ({}, {"available": False, "age_s": None})


def test_env_override_path(tmp_path, monkeypatch):
    path = _write(tmp_path, ts=5.0, pids={})
    monkeypatch.setenv("NOUS_NET_BY_PID", path)
    _, probe = monitor._read_net_by_pid(now=6.0)
    assert probe["available"] is True


class _FakeProc:
    def __init__(self, pid, cpu, rss_mb, name="p"):
        self.info = {
            "pid": pid, "name": name, "cpu_percent": cpu,
            "memory_info": SimpleNamespace(rss=rss_mb * 1024**2), "cmdline": [name, "--x"],
        }


def _fake_iter(procs):
    def it(attrs=None):
        return iter(procs)
    return it


def test_top_processes_attaches_rates_and_null_when_unavailable():
    procs = [_FakeProc(1, 50.0, 100), _FakeProc(2, 10.0, 50)]
    with patch.object(psutil, "process_iter", _fake_iter(procs)):
        rows = monitor._top_processes(limit=20, net={1: (123, 45)})
        assert rows[0]["pid"] == 1 and rows[0]["net_tx_bps"] == 123 and rows[0]["net_rx_bps"] == 45
        assert rows[1]["net_tx_bps"] == 0 and rows[1]["net_rx_bps"] == 0
        rows = monitor._top_processes(limit=20, net=None)
        assert rows[0]["net_tx_bps"] is None and rows[0]["net_rx_bps"] is None


def test_top_processes_unions_net_pids_outside_cpu_top_and_caps_total():
    # 25 个 CPU 进程(cpu 递减),再 30 个只有流量的 pid → 前 20 CPU + 流量补位,总数 ≤ 40
    procs = [_FakeProc(i, 100.0 - i, 10) for i in range(1, 26)]
    procs += [_FakeProc(100 + i, 0.0, 10, name=f"net{i}") for i in range(30)]
    net = {100 + i: (1000 * (30 - i), 0) for i in range(30)}
    with patch.object(psutil, "process_iter", _fake_iter(procs)):
        rows = monitor._top_processes(limit=20, net=net, max_total=40)
    pids = [r["pid"] for r in rows]
    assert pids[:20] == list(range(1, 21))              # CPU 前 20 原样
    assert len(rows) == 40                               # 补到上限
    assert pids[20] == 100                               # 流量最大的先补
    assert 25 not in pids                                # 既不在 CPU 前 20 也没流量 → 不进


async def test_stats_exposes_net_probe(db_client, tmp_path, monkeypatch):
    monkeypatch.setenv("NOUS_NET_BY_PID", str(tmp_path / "missing.json"))
    with patch("src.api.routes.monitor._gpu_stats_nvidia_smi", return_value=[]), \
         patch("src.api.routes.monitor._gpu_processes", return_value={}), \
         patch("src.api.routes.monitor._top_processes", return_value=[]), \
         patch("src.services.gpu_monitor.DEFAULT_RESERVED_GB", 4.0):
        resp = await db_client.get("/api/v1/monitor/stats")
    assert resp.status_code == 200
    assert resp.json()["net_probe"] == {"available": False, "age_s": None}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run pytest tests/test_monitor_net_probe.py -q`
Expected: FAIL(`AttributeError: _read_net_by_pid` / `TypeError: unexpected keyword 'net'`)。

- [ ] **Step 3: 实现**

`backend/src/api/routes/monitor.py`——在 import 区加 `import json`、`import os`;在 `_top_processes` 上方加:

```python
# spec 2026-09-06 process-net-traffic §2.2:每进程网络速率来自 root 采集器
# (nous-engine-netprobe)每 2s 原子写的文件;本进程(heygo)只读,不提权。
NET_BY_PID_DEFAULT = "/run/nous-engine/net_by_pid.json"
NET_BY_PID_MAX_AGE_S = 10.0
_net_probe_last_available: bool | None = None


def _read_net_by_pid(
    path: str | None = None,
    max_age_s: float = NET_BY_PID_MAX_AGE_S,
    now: float | None = None,
) -> tuple[dict[int, tuple[int, int]], dict]:
    """读采集器文件 → ({pid: (tx_bps, rx_bps)}, net_probe)。缺失/过期/坏 JSON 一律
    ({}, available=False),不抛;每 2s 轮询都会调,只在可用性**翻转**时记一行 info。"""
    global _net_probe_last_available
    path = path or os.environ.get("NOUS_NET_BY_PID", NET_BY_PID_DEFAULT)
    now = time.time() if now is None else now
    net: dict[int, tuple[int, int]] = {}
    available = False
    age: float | None = None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        age = round(now - float(doc["ts"]), 1)
        interval = float(doc.get("interval_s") or 2) or 2.0
        if 0 <= age <= max_age_s:
            available = True
            for pid_s, v in (doc.get("pids") or {}).items():
                try:
                    net[int(pid_s)] = (int(int(v.get("tx", 0)) / interval), int(int(v.get("rx", 0)) / interval))
                except (TypeError, ValueError, AttributeError):
                    continue
    except (OSError, ValueError, KeyError, TypeError):
        net, available, age = {}, False, None
    if available != _net_probe_last_available:
        logger.info("netprobe %s(%s)", "可用" if available else "不可用", path)
        _net_probe_last_available = available
    return net, {"available": available, "age_s": age}
```

把 `_top_processes` 改成:

```python
def _top_processes(
    limit: int = 20,
    net: dict[int, tuple[int, int]] | None = None,
    max_total: int = 40,
) -> list[dict]:
    """CPU 前 limit 个进程 ∪ 有网络流量但不在其中的进程(按流量降序补,总数 ≤ max_total)。
    net 为 None = 采集器不可用 → net_*_bps 全 None;为 {} = 可用但没流量 → 全 0。"""
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "cmdline"]):
        try:
            info = p.info
            procs.append(
                {
                    "pid": info["pid"],
                    "name": info["name"] or "",
                    "cpu_percent": info["cpu_percent"] or 0.0,
                    "memory_mb": round((info["memory_info"].rss if info["memory_info"] else 0) / 1024**2),
                    "command": " ".join(info["cmdline"][:5]) if info["cmdline"] else info["name"] or "",
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    procs.sort(key=lambda x: x["cpu_percent"], reverse=True)
    top = procs[:limit]
    if net:
        chosen = {r["pid"] for r in top}
        by_pid = {r["pid"]: r for r in procs}
        extra = sorted(
            (pid for pid in net if pid not in chosen and pid in by_pid and sum(net[pid]) > 0),
            key=lambda pid: sum(net[pid]),
            reverse=True,
        )
        top = top + [by_pid[pid] for pid in extra[: max(0, max_total - len(top))]]
    for r in top:
        if net is None:
            r["net_tx_bps"] = None
            r["net_rx_bps"] = None
        else:
            tx, rx = net.get(r["pid"], (0, 0))
            r["net_tx_bps"] = tx
            r["net_rx_bps"] = rx
    return top
```

`_compute_system_stats` 里原来的
`processes = await asyncio.to_thread(_top_processes)` 改为:

```python
    # spec process-net-traffic §2.2:读采集器文件 + 扫进程都在同一个线程里,不占事件循环。
    def _procs_with_net():
        net, probe = _read_net_by_pid()
        return _top_processes(net=net if probe["available"] else None), probe

    processes, net_probe = await asyncio.to_thread(_procs_with_net)
```

响应字典里 `"processes": processes,` 下面加一行 `"net_probe": net_probe,`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && uv run pytest tests/test_monitor_net_probe.py tests/test_api_monitor.py -q`
Expected: 全部 passed(`test_api_monitor.py` 里 patch 了 `_top_processes` 的用例不受影响)。

- [ ] **Step 5: 全量**

Run: `cd backend && uv run pytest tests -n 8 -q --ignore=tests/test_integration_smoke.py --ignore=tests/test_tts_engine_adapter.py`
Expected: 全绿。

- [ ] **Step 6: Commit + PR**

```bash
git checkout -b feat/monitor-net-by-pid master
git add backend/src/api/routes/monitor.py backend/tests/test_monitor_net_probe.py
git -c commit.gpgsign=false commit -m "feat(monitor): 进程表并入 netprobe 每进程网络速率 + 响应加 net_probe"
git push -u origin feat/monitor-net-by-pid && gh pr create --base master --fill
```

---

### Task 3: 前端 — 三列可排序 + 「网络」列

**Files:**
- Create: `frontend/src/components/overlays/processSort.ts`
- Test: `frontend/src/components/overlays/processSort.test.ts`
- Modify: `frontend/src/api/system.ts`(`ProcessInfo` 60-66 行;`MonitorStatsResponse` 68-73 行;`useSysProcesses` 100-103 行)
- Modify: `frontend/src/components/overlays/DashboardOverlay.tsx`(进程表 735-785 行;组件顶部 `useState`)

**Interfaces:**
- Consumes: Task 2 的 `net_tx_bps`/`net_rx_bps`(`number | null`)与 `net_probe`。
- Produces: `sortProcesses(rows: ProcessInfo[], key: ProcSortKey, dir: 'asc' | 'desc'): ProcessInfo[]`、`formatRate(bps: number | null): string`、`type ProcSortKey = 'cpu' | 'mem' | 'net'`。

- [ ] **Step 1: 写失败测试**

`frontend/src/components/overlays/processSort.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { sortProcesses, formatRate, type ProcSortKey } from './processSort'
import type { ProcessInfo } from '../../api/system'

function row(pid: number, cpu: number, mem: number, tx: number | null, rx: number | null): ProcessInfo {
  return { pid, name: `p${pid}`, cpu_percent: cpu, memory_mb: mem, command: '', net_tx_bps: tx, net_rx_bps: rx }
}

const rows = [row(1, 5, 300, 10, 0), row(2, 50, 100, null, null), row(3, 20, 200, 500, 700)]

describe('sortProcesses', () => {
  it('cpu desc 是默认视角', () => {
    expect(sortProcesses(rows, 'cpu', 'desc').map((r) => r.pid)).toEqual([2, 3, 1])
  })
  it('mem asc', () => {
    expect(sortProcesses(rows, 'mem', 'asc').map((r) => r.pid)).toEqual([2, 3, 1])
  })
  it('net 按 tx+rx,null 当 0,desc', () => {
    expect(sortProcesses(rows, 'net', 'desc').map((r) => r.pid)).toEqual([3, 1, 2])
  })
  it('不改原数组', () => {
    const copy = [...rows]
    sortProcesses(rows, 'net' as ProcSortKey, 'desc')
    expect(rows).toEqual(copy)
  })
})

describe('formatRate', () => {
  it('null → —', () => expect(formatRate(null)).toBe('—'))
  it('< 1KB 用 B/s', () => expect(formatRate(512)).toBe('512 B/s'))
  it('< 1MB 用 KB/s 一位小数', () => expect(formatRate(12_600)).toBe('12.3 KB/s'))
  it('≥ 1MB 用 MB/s 一位小数', () => expect(formatRate(5 * 1024 * 1024)).toBe('5.0 MB/s'))
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/components/overlays/processSort.test.ts`
Expected: FAIL(找不到 `./processSort`)。

- [ ] **Step 3: 改类型与 hook**

`frontend/src/api/system.ts`:

```ts
export interface ProcessInfo {
  pid: number
  name: string
  cpu_percent: number
  memory_mb: number
  command: string
  // spec 2026-09-06 process-net-traffic:采集器不可用时为 null(不是 0)。
  net_tx_bps: number | null
  net_rx_bps: number | null
}

export interface NetProbeInfo {
  available: boolean
  age_s: number | null
}

interface MonitorStatsResponse {
  gpus: SysGpuResponse
  system: SystemStats
  processes: ProcessInfo[]
  net_probe: NetProbeInfo
  uptime_seconds: number
}
```

`useSysProcesses` 改为:

```ts
export function useSysProcesses() {
  const q = useMonitorStats()
  return {
    ...q,
    data: q.data ? { processes: q.data.processes, net_probe: q.data.net_probe } : undefined,
  }
}
```

- [ ] **Step 4: 写纯函数**

`frontend/src/components/overlays/processSort.ts`:

```ts
import type { ProcessInfo } from '../../api/system'

export type ProcSortKey = 'cpu' | 'mem' | 'net'
export type ProcSortDir = 'asc' | 'desc'

function keyOf(r: ProcessInfo, key: ProcSortKey): number {
  switch (key) {
    case 'cpu':
      return r.cpu_percent
    case 'mem':
      return r.memory_mb
    case 'net':
      return (r.net_tx_bps ?? 0) + (r.net_rx_bps ?? 0)
  }
}

/** 不改原数组;同值按 pid 稳定。 */
export function sortProcesses(rows: ProcessInfo[], key: ProcSortKey, dir: ProcSortDir): ProcessInfo[] {
  const sign = dir === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    const d = keyOf(a, key) - keyOf(b, key)
    return d !== 0 ? sign * d : a.pid - b.pid
  })
}

/** 字节/秒 → 人看的速率;null(采集器不可用)→ '—'。 */
export function formatRate(bps: number | null): string {
  if (bps === null || bps === undefined) return '—'
  if (bps < 1024) return `${Math.round(bps)} B/s`
  if (bps < 1024 * 1024) return `${(bps / 1024).toFixed(1)} KB/s`
  return `${(bps / 1024 / 1024).toFixed(1)} MB/s`
}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/components/overlays/processSort.test.ts`
Expected: 8 passed。

- [ ] **Step 6: 改表格**

`DashboardOverlay.tsx`:顶部 import 加
`import { sortProcesses, formatRate, type ProcSortKey, type ProcSortDir } from './processSort'`;
组件内(`const { data: procData } = useSysProcesses()` 之后)加:

```tsx
  const [procSort, setProcSort] = useState<{ key: ProcSortKey; dir: ProcSortDir }>({ key: 'cpu', dir: 'desc' })
  const toggleProcSort = (key: ProcSortKey) =>
    setProcSort((s) => (s.key === key ? { key, dir: s.dir === 'desc' ? 'asc' : 'desc' } : { key, dir: 'desc' }))
  const sortedProcs = procData ? sortProcesses(procData.processes, procSort.key, procSort.dir) : []
  const netProbeOk = procData?.net_probe?.available ?? false
  const sortMark = (key: ProcSortKey) => (procSort.key === key ? (procSort.dir === 'desc' ? ' ▼' : ' ▲') : '')
  const thBtn: React.CSSProperties = { padding: '4px 8px', cursor: 'pointer', userSelect: 'none' }
```

表头三列改成可点(PID / NAME / COMMAND 不动):

```tsx
                    <th style={{ padding: '4px 8px' }}>PID</th>
                    <th style={thBtn} onClick={() => toggleProcSort('cpu')}>CPU%{sortMark('cpu')}</th>
                    <th style={thBtn} onClick={() => toggleProcSort('mem')}>MEM{sortMark('mem')}</th>
                    <th
                      style={thBtn}
                      onClick={() => toggleProcSort('net')}
                      title={netProbeOk ? '每进程 TCP/UDP 收发速率(采集器 nous-engine-netprobe)' : '采集器未运行:sudo ./infra/systemd/install.sh'}
                    >
                      网络{sortMark('net')}
                    </th>
                    <th style={{ padding: '4px 8px' }}>NAME</th>
                    <th style={{ padding: '4px 8px' }}>COMMAND</th>
```

行渲染 `procData.processes.slice(0, 15).map((p) => (` 改为 `sortedProcs.slice(0, 15).map((p) => (`,在 MEM 单元格后加:

```tsx
                      <td style={{ padding: '3px 8px', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                        {p.net_tx_bps === null ? '—' : `▲ ${formatRate(p.net_tx_bps)} ▼ ${formatRate(p.net_rx_bps)}`}
                      </td>
```

若文件顶部没有 `import type React from 'react'`(`React.CSSProperties` 要用),把 `thBtn` 的类型改写为 `{ padding: string; cursor: 'pointer'; userSelect: 'none' }` 字面量对象也行——以 `tsc -b` 通过为准。

- [ ] **Step 7: 类型检查 + 测试 + lint**

Run: `cd frontend && npx tsc -b && npx vitest run && npm run lint`
Expected: 无错误(`tsc -b` 是 project references 构建,别用裸 `npx tsc`)。

- [ ] **Step 8: Commit + PR**

```bash
git checkout -b feat/process-table-net-sort master
git add frontend/src/components/overlays/processSort.ts frontend/src/components/overlays/processSort.test.ts frontend/src/api/system.ts frontend/src/components/overlays/DashboardOverlay.tsx
git -c commit.gpgsign=false commit -m "feat(dashboard): 进程表 CPU/MEM/网络三列可排序 + 每进程网络速率列"
git push -u origin feat/process-table-net-sort && gh pr create --base master --fill
```

(Task 3 可与 Task 2 并行开发,但合并顺序 Task 2 先:前端字段来自后端。)

---

### Task 4: 安装接线 — install.sh / target / enginectl / README + 清 healthprobe drop-in

**Files:**
- Modify: `infra/systemd/install.sh`(数组 18-26 行;单元循环 57-60;自检段 99-104 附近;uninstall 段)
- Modify: `infra/systemd/nous-engine.target`(`Wants=` 行)
- Modify: `infra/systemd/enginectl`(`STACK_UNITS` 30 行;`cmd_logs` 的 case;用法行)
- Modify: `infra/systemd/README.md`(设计选择列表加一条)

**Interfaces:**
- Consumes: Task 1 的 `infra/systemd/nous-engine-netprobe.service`、`infra/monitoring/netprobe.bt`。
- Produces: `sudo ./infra/systemd/install.sh` 装并启采集器(有 bpftrace 时)。

- [ ] **Step 1: install.sh**

数组区:`UNIT_FILES=(...)` 里加 `nous-engine-netprobe.service`(放 `nous-engine.target` 之前);`SERVICES` **不**加(它按条件启)。

在「── 2. 启用 + 启动长驻服务」循环之后、cloudflared 退役块之前插入:

```bash
    # netprobe(spec 2026-09-06 process-net-traffic):root bpftrace 采集每进程网络流量。
    # 机器上有 bpftrace 才装;先 --dry-run 附着一次全部探针,符号缺失就明说、不启。
    if command -v bpftrace >/dev/null 2>&1; then
      if bpftrace --dry-run "$SCRIPT_DIR/../monitoring/netprobe.bt" >/tmp/nous-netprobe-dryrun.log 2>&1; then
        if systemctl enable --now nous-engine-netprobe.service >/dev/null 2>&1; then ok "nous-engine-netprobe(每进程网络流量)"
        else bad "nous-engine-netprobe 启动失败 — 查 journalctl -u nous-engine-netprobe -n 50"; fi
      else
        systemctl disable --now nous-engine-netprobe.service >/dev/null 2>&1 || true
        bad "netprobe 探针附着失败(内核符号缺?)— 见 /tmp/nous-netprobe-dryrun.log;面板「网络」列将显示 —"
      fi
    else
      systemctl disable --now nous-engine-netprobe.service >/dev/null 2>&1 || true
      warn "未装 bpftrace → 跳过 nous-engine-netprobe(面板「网络」列显示 —);apt install bpftrace 后重跑本脚本"
    fi

    # 隧道退役后 healthprobe 的 NOUS_TUNNEL_AUTOHEAL drop-in 已无意义,顺手清掉。
    if [[ -d "$TARGET/nous-engine-healthprobe.service.d" ]]; then
      rm -rf "$TARGET/nous-engine-healthprobe.service.d"; systemctl daemon-reload
      ok "已清除 nous-engine-healthprobe.service.d(隧道自愈 drop-in,已失效)"
    fi
```

自检段 `for svc in postgresql "${SERVICES[@]}"; do` 那一行改为
`for svc in postgresql "${SERVICES[@]}" nous-engine-netprobe.service; do`(未启用时按既有格式显示 inactive 即可)。

uninstall 段 `for svc in "${SERVICES[@]}"; do ...` 后加一行:
`systemctl disable --now nous-engine-netprobe.service 2>/dev/null || true; rm -f "$TARGET/nous-engine-netprobe.service"; ok "移除 nous-engine-netprobe.service"`

- [ ] **Step 2: target + enginectl + README**

`nous-engine.target`:`Wants=postgresql.service nous-engine-backend.service nous-engine-status.service nous-engine-netprobe.service`,Description 改「DB + 后端 + 状态 + 网络采集」。

`enginectl`:`STACK_UNITS=(postgresql nous-engine-backend nous-engine-status nous-engine-netprobe nous-engine-comfyui)`;`cmd_logs` 的 case 第一行改为 `backend|status|healthprobe|netprobe|comfyui) journalctl -u "nous-engine-$u" -f ;;`,未知目标提示与用法行同步加 `netprobe`。`APP_UNITS` **不**加(采集器不随后端 up/down/restart,常驻即可)。

`infra/systemd/README.md` 「设计选择」列表加:

```markdown
- **`nous-engine-netprobe`(每进程网络流量,root)** — `infra/monitoring/netprobe.bt` 用 bpftrace
  的 kretprobe 按 pid 累加 TCP/UDP 实际收发字节(只有字节数,没有地址/端口/内容),包装器
  `nous-netprobe.py` 每 2s 原子写 `/run/nous-engine/net_by_pid.json`;后端(heygo)只读它,不提权。
  面板「进程」表的「网络」列与按流量排序来自这里;采集器不在时该列显示「—」。有 bpftrace
  才装(`install.sh` 先 `--dry-run` 附着探针,符号缺失就不启并明说)。spec
  `docs/superpowers/specs/2026-09-06-process-net-traffic-design.md`。
```

- [ ] **Step 3: 静态检查**

Run: `bash -n infra/systemd/install.sh && bash -n infra/systemd/enginectl && grep -n "netprobe" infra/systemd/install.sh infra/systemd/nous-engine.target infra/systemd/enginectl | wc -l`
Expected: 无语法错;计数 ≥ 8。

- [ ] **Step 4: Commit + PR**

```bash
git checkout -b infra/netprobe-install master
git add infra/systemd/install.sh infra/systemd/nous-engine.target infra/systemd/enginectl infra/systemd/README.md
git -c commit.gpgsign=false commit -m "infra: install.sh 装 nous-engine-netprobe(有 bpftrace 才装,dry-run 自检)+ 纳入 target/enginectl;清 healthprobe 隧道 drop-in"
git push -u origin infra/netprobe-install && gh pr create --base master --fill
```

---

### Task 5: 真机验收(用户敲 sudo,控制器核验)

**Files:** 无代码;结果记到 spec 末尾新增「§9 验收结果」并开 docs PR。

- [ ] **Step 1: 用户敲**:`sudo ./infra/systemd/install.sh`(四个 PR 全合并、工作树在 master 之后)。
- [ ] **Step 2: 核验采集器**:`systemctl is-active nous-engine-netprobe` = active;`ls -la /run/nous-engine/net_by_pid.json` 存在且 mtime 每 2s 变;`python3 -c "import json;d=json.load(open('/run/nous-engine/net_by_pid.json'));print(d['ts'],len(d['pids']))"`。
- [ ] **Step 3: 核验后端**:`curl -s -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8000/api/v1/monitor/stats | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['net_probe']);print([(p['pid'],p['name'],p['net_tx_bps'],p['net_rx_bps']) for p in d['processes'][:5]])"` → `available: True`,速率为整数。
- [ ] **Step 4: 制造流量看归属**:另开终端 `curl -s -o /dev/null https://hf-mirror.com/BennyDaBall/Z-Image-Engineer-V6-GGUF/resolve/main/Z-Image-Engineer-V6-Q2_K.gguf`(跑 10s 后 Ctrl-C),期间再查 Step 3,`curl` 进程应出现且 `net_rx_bps` 为 MB/s 级;docker 容器进程(如 `nous-cloudflared` 的 cloudflared)也应有非零值。
- [ ] **Step 5: 面板**:`cd frontend && npm run build` 后(后端 serve dist)打开管理台系统状态页:点「网络」表头按流量降序,`▼` 标记出现;再点切升序;「CPU%」「MEM」同理。
- [ ] **Step 6: 记录**:spec 加「§9 验收结果」(命令与输出摘要),docs PR 合并;`enginectl status` 输出贴进去。

---

## Self-Review

- **Spec 覆盖**:§2.1 采集器 → Task 1 + 4;§2.2 后端 → Task 2;§2.3 前端 → Task 3;§3 错误处理(bpftrace 缺/符号缺/崩溃/坏 JSON/pid 退出)→ Task 1 包装器 + Task 2 `_read_net_by_pid`/`_top_processes` 跳过 + Task 4 install 分支;§4 安全边界 → Task 1 单元 `ProtectSystem=strict` + 脚本只累加字节;§5 测试 → 各 Task 的 Step 1;§7 前置探针验证 → 改为 kretprobe 稳定符号 + install.sh `--dry-run` 自检(Task 1 Step 7、Task 4 Step 1);§8 清 drop-in → Task 4。
- **占位扫描**:无 TBD/TODO;每个代码步骤有完整代码。
- **类型一致**:`_read_net_by_pid` 返回 `(dict[int, tuple[int,int]], {"available","age_s"})` 在 Task 2 测试与实现一致;`net_tx_bps`/`net_rx_bps` `int | None` ↔ 前端 `number | null`;`net_probe` 键名前后端一致;`sortProcesses`/`formatRate`/`ProcSortKey` 名称在 Task 3 三处一致。
- **spec 偏差(记录)**:spec §2.1 写的是 `kprobe:tcp_sendmsg arg2` / `kprobe:tcp_cleanup_rbuf arg1`;计划改用 **kretprobe 返回值**(实际字节,且 `tcp_recvmsg` 等为稳定导出符号,降低内核 7.0 符号缺失风险)。语义相同(按 pid 的实际收发字节),精度更高。
