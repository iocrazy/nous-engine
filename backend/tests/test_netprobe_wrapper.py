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


# ── bpftrace 脚本的静态守卫(2026-09-07 真机验收发现的 bug)────────────────


def _bt_source() -> str:
    return (_REPO / "infra/monitoring/netprobe.bt").read_text(encoding="utf-8")


def test_bt_truncates_retval_to_signed_int32():
    """kretprobe 的 retval 必须先截成 32 位有符号再判正负。

    tcp_recvmsg / udp_recvmsg / tcp_sendmsg 这些内核函数返回的是 `int`(32 位),
    而 bpftrace 的 retval 是 64 位且**零扩展**:负的 errno(-EAGAIN = -11)会变成
    4294967285 这种巨大正数,直接冲过 `retval > 0`,每次给该 pid 累加约 4.29GB。

    2026-09-07 真机实测(生产机 /run/nous-engine/net_by_pid.json):一个 2 秒区间的
    67 个 pid 里有 26 个 rx > 4GB;拆开正是「高 32 位 = 负返回次数,低 32 位 = 2^32 - errno」。
    """
    bt = _bt_source()
    assert "(int32)" in bt, "retval 必须先 (int32) 截断,否则零扩展的负 errno 会被当成巨量流量"
    bare = [
        ln.strip()
        for ln in bt.splitlines()
        if "retval > 0" in ln and "(int32)" not in ln and not ln.strip().startswith("//")
    ]
    assert not bare, f"还有裸的 `retval > 0` 判断(必须先截 int32):{bare}"


def test_bt_accumulates_only_the_truncated_value():
    """累加进 map 的也必须是截断后的值,不能是原始 retval。"""
    bt = _bt_source()
    adds = [ln.strip() for ln in bt.splitlines() if "+=" in ln and not ln.strip().startswith("//")]
    assert adds, "netprobe.bt 里找不到累加语句"
    for ln in adds:
        assert "retval" not in ln, f"累加用的是原始 retval(零扩展)而不是截断值:{ln}"
