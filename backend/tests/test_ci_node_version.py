"""CI 的 Node 版本必须满足前端依赖声明的最低 engines(2026-09-09,#734)。

dependabot 把 jsdom 29→30 / vitest 4→5 / undici 7→8 一起抬上来,三者 engines 都要求
Node ≥ 22(undici 8 直接 `require('node:worker_threads').markAsUncloneable`,22.10 才有),
而 ci.yml 还钉着 `node-version: 20` → vitest 0 个测试收集到、67 个 unhandled error。
本地(Node 26)全绿,只在 CI 上红 —— 这种「环境差异」型故障该在合并前就本地可见。

只比较主版本:engines 里的次版本门槛(如 jsdom 的 ^22.22.2)由 setup-node 解析
`node-version: <major>` 取该大版本最新来满足。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

#: 测试链路上对 Node 版本最敏感的三个包 —— 也是 #734 实际炸的那三个。
_WATCHED = ("undici", "jsdom", "vitest")


def _ci_frontend_node_major() -> int:
    ci = (_REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    majors = [int(m) for m in re.findall(r"node-version:\s*['\"]?(\d+)", ci)]
    assert majors, "ci.yml 里找不到 node-version"
    assert len(set(majors)) == 1, f"ci.yml 里 node-version 不一致:{majors}"
    return majors[0]


def _min_major(engines_node: str) -> int:
    """`^22.22.2 || ^24.15.0 || >=26.0.0` → 22;`>=22.19.0` → 22。取各备选里最低的主版本。"""
    alts = [a.strip() for a in engines_node.split("||")]
    majors = []
    for a in alts:
        m = re.search(r"(\d+)", a)
        assert m, f"看不懂的 engines 表达式:{engines_node!r}"
        majors.append(int(m.group(1)))
    return min(majors)


def _locked_engines() -> dict[str, str]:
    lock = json.loads(
        (_REPO / "frontend/package-lock.json").read_text(encoding="utf-8")
    )
    out = {}
    for name in _WATCHED:
        entry = lock["packages"].get(f"node_modules/{name}")
        assert entry, f"package-lock 里没有 node_modules/{name}"
        node_range = (entry.get("engines") or {}).get("node")
        if node_range:
            out[name] = node_range
    return out


def test_ci_node_satisfies_frontend_dev_deps():
    ci_major = _ci_frontend_node_major()
    engines = _locked_engines()
    assert engines, "锁文件里三个包都没 engines?npm lockfile v3 应该带"
    too_low = {name: rng for name, rng in engines.items() if ci_major < _min_major(rng)}
    assert not too_low, (
        f"ci.yml 的 node-version: {ci_major} 低于依赖要求:{too_low} —— "
        "升 ci.yml 的 node-version(并同步 frontend/package.json 的 engines)"
    )


def test_package_json_declares_engines_matching_ci():
    """package.json 的 engines 是给人和 npm 看的同一份事实;缺了或落后于 CI 都算漂移。"""
    pkg = json.loads((_REPO / "frontend/package.json").read_text(encoding="utf-8"))
    node_range = (pkg.get("engines") or {}).get("node")
    assert node_range, "frontend/package.json 缺 engines.node"
    assert _min_major(node_range) <= _ci_frontend_node_major()
    for name, rng in _locked_engines().items():
        assert _min_major(node_range) >= _min_major(rng), (
            f"package.json engines {node_range!r} 低于 {name} 的 {rng!r}"
        )
