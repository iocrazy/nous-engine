"""主进程聚合「已加载 adapter」单一真相来源。

tts adapter 真加载在各 runner 子进程自己的 `ModelManager._models`,主进程的
`app.state.model_manager._models` 看不到它们 —— 不聚合的话系统状态「已加载模型(N)」、
`GET /api/v1/engines/loaded-adapters` 会恒为 0/空。

runner 每个 Pong 带回 `loaded_models_snapshot()`(supervisor watchdog 每 ping 对账一次,
存在 `RunnerSupervisor.loaded_models`)。本模块把所有 runner 的快照 + 主进程自身
`_models`(若有 in-process 模型)聚合成一份带 `group_id` 的列表,供上述视图读。

跨进程,所以是 best-effort 镜像:延迟 ≤ 一个 ping_interval,主进程重启后下个 ping 重填。
"""
from __future__ import annotations

import os
from typing import Any


def aggregate_runner_loaded(app_state: Any) -> list[dict]:
    """汇总所有 runner 子进程上报的已加载 adapter 快照 + 主进程自身 _models。

    每条 entry 形如 ModelManager.loaded_models_snapshot() 的 dict,额外带 `group_id`
    (runner group 如 'tts',或主进程 'main')。
    """
    out: list[dict] = []
    sups = getattr(app_state, "runner_supervisors", None)
    if isinstance(sups, list):
        for sup in sups:
            gid = getattr(sup, "group_id", "?")
            entries = getattr(sup, "loaded_models", None)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        out.append({**entry, "group_id": gid})
    mgr = getattr(app_state, "model_manager", None)
    snap = getattr(mgr, "loaded_models_snapshot", None)
    if callable(snap):
        try:
            entries = snap()
        except Exception:  # noqa: BLE001 — mock / 异常 mgr 不该拖垮聚合
            entries = None
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    out.append({**entry, "group_id": "main"})
    return out


def _norm(path: str) -> str:
    """文件路径归一化到可比较的基名(去目录/扩展名,小写)。

    引擎库卡片代表一个组件文件或模型目录(如 TTS 的 checkpoint 目录),而 loaded
    adapter 的 source_files 是真实绝对路径;靠 basename(不含扩展名)做宽松匹配。
    """
    base = os.path.basename(path.rstrip("/"))
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return stem.strip().lower()


def loaded_source_stems(app_state: Any) -> set[str]:
    """所有已加载 adapter 的源组件文件归一化基名集合 —— 给引擎库卡片判 loaded 用。"""
    stems: set[str] = set()
    for entry in aggregate_runner_loaded(app_state):
        for f in entry.get("source_files") or []:
            if f:
                stems.add(_norm(f))
    return stems
