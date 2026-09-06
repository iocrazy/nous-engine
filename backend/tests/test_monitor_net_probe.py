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


@pytest.fixture(autouse=True)
def _clear_stats_cache():
    """/monitor/stats 有 1.5s TTL 缓存(模块级,跨测试文件共享)。不清的话本文件的
    stats 用例可能拿到别处用例刚写进去的、没有 net_probe 的旧 payload。"""
    monitor._reset_stats_cache()
    yield
    monitor._reset_stats_cache()


async def test_stats_exposes_net_probe(db_client, tmp_path, monkeypatch):
    monkeypatch.setenv("NOUS_NET_BY_PID", str(tmp_path / "missing.json"))
    with patch("src.api.routes.monitor._gpu_stats_nvidia_smi", return_value=[]), \
         patch("src.api.routes.monitor._gpu_processes", return_value={}), \
         patch("src.api.routes.monitor._top_processes", return_value=[]), \
         patch("src.services.gpu_monitor.DEFAULT_RESERVED_GB", 4.0):
        resp = await db_client.get("/api/v1/monitor/stats")
    assert resp.status_code == 200
    assert resp.json()["net_probe"] == {"available": False, "age_s": None}
