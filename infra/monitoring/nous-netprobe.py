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
