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
