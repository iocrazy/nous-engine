"""巡检脚本 nous-healthprobe.sh 对 /health 的 comfy 块的处理(2026-09-26)。

静态守卫(同 test_infra_privilege_boundary 的风格):脚本解析了 comfy,且 ComfyUI
问题只 WARN、不 ALERT —— 用户只要看得见、不接通知,且 ComfyUI 挂不等于后端硬故障
(ALERT 会让单元 exit 1 / 标 failed)。再把脚本里内嵌的 python 解析段抠出来喂几个
/health 样本,确认日志行里带 problems 原文。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "infra/monitoring/nous-healthprobe.sh"


def _script() -> str:
    assert _SCRIPT.is_file(), f"缺文件 {_SCRIPT}"
    return _SCRIPT.read_text(encoding="utf-8")


def _embedded_parser() -> str:
    m = re.search(r"python3 -c '\n(.*?)\n' 2>/dev/null", _script(), re.S)
    assert m, "找不到 healthprobe 内嵌的 python 解析段"
    return m.group(1)


def _parse(health: dict) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-c", _embedded_parser()],
        input=json.dumps(health), capture_output=True, text=True, timeout=10, check=True,
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def test_probe_reads_comfy_block():
    src = _embedded_parser()
    assert 'd.get("comfy")' in src
    assert '"problems"' in src


def test_comfy_is_warn_never_alert():
    src = _embedded_parser()
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    comfy_lines = [ln for ln in code if "comfy" in ln.lower()]
    assert any("WARN comfy-" in ln for ln in comfy_lines)
    assert not any("ALERT" in ln for ln in comfy_lines)


def test_comfy_degraded_logs_problems_verbatim():
    lines = _parse({"database": "ok", "comfy": {"state": "degraded", "problems": [
        "占用 8888 的不是 systemd 管理的实例(pid 4242)", "缺少监听地址 100.124.149.118"]}})
    assert lines == [
        "WARN comfy-degraded(占用 8888 的不是 systemd 管理的实例(pid 4242);缺少监听地址 100.124.149.118)"]


def test_comfy_down_is_warn():
    lines = _parse({"database": "ok", "comfy": {"state": "down", "problems": ["ComfyUI 未响应(:8888)"]}})
    assert lines == ["WARN comfy-down(ComfyUI 未响应(:8888))"]


def test_comfy_ok_or_absent_is_silent():
    assert _parse({"database": "ok", "comfy": {"state": "ok", "problems": []}}) == []
    # 旧后端没有 comfy 字段 → 不报
    assert _parse({"database": "ok"}) == []
