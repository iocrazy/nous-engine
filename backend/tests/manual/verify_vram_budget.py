"""真机验证:每模型显存预算 absolute 模式(spec 2026-06-13,#532)。

设 qwen3_embedding_4b 显存预算 = 绝对 11GB → 加载 vLLM(pooling)到 cuda:1(Pro6000 96G)
→ 读 nvidia-smi 实占,确认 ≈11GB(对比 yaml 默认 util=0.2 的 ≈19GB)。

CUDA_DEVICE_ORDER=PCI_BUS_ID 必须在 import 前设(CLAUDE.md):否则 torch 默认 FASTEST_FIRST
把 cuda:1 排成 3090,11GB 也许装得下但卡选错验不出 96G 换算。

跑法(uv 不 load .env,先 source):
    cd backend && set -a && source .env && set +a && \
        uv run python tests/manual/verify_vram_budget.py
"""
import os

os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/ 进 sys.path

from src.services.inference.llm_vllm import VLLMAdapter

MODEL_PATH = "embedding/Qwen3-Embedding-4B"
DEVICE = "cuda:1"  # Pro6000 96G(PCI_BUS_ID 序)
BUDGET_GB = 11.0
CARD_GB = 96.0


def _proc_vram_mb(root_pid: int) -> int:
    """nvidia-smi 里 root_pid 及其子孙占用的显存(MB)合计。"""
    pids = {root_pid}
    try:
        out = subprocess.run(
            ["ps", "-o", "pid", "--no-headers", "--ppid", str(root_pid)],
            capture_output=True, text=True, timeout=3,
        ).stdout
        for line in out.splitlines():
            s = line.strip()
            if s.isdigit():
                pids.add(int(s))
    except Exception:
        pass
    total = 0
    res = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5,
    )
    for line in res.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) in pids:
            total += int(parts[1])
    return total


async def main() -> int:
    print(f"[verify] 设 {MODEL_PATH} 显存预算 = 绝对 {BUDGET_GB}GB → 预期 util≈{BUDGET_GB / CARD_GB:.3f}")
    adapter = VLLMAdapter(
        paths={"main": MODEL_PATH},
        vllm_runner="pooling",
        max_model_len=8192,
        vram_budget={"mode": "absolute", "value": BUDGET_GB},
    )
    print(f"[verify] 加载到 {DEVICE}（首次可能编译 kernel，最多 ~10 分钟）…")
    await adapter.load(device=DEVICE)
    pid = adapter._process.pid if adapter._process else None
    print(f"[verify] vLLM 已就绪 pid={pid} base_url={adapter.base_url}")

    used_mb = _proc_vram_mb(pid) if pid else 0
    used_gb = used_mb / 1024
    print(f"[verify] nvidia-smi 实占 = {used_gb:.2f} GB（预算 {BUDGET_GB}GB；yaml 默认 0.2→≈19.2GB）")

    # vLLM 按 utilization 预留整段(权重+激活+KV),实占应接近预算上限。容差 ±1.5GB。
    ok = abs(used_gb - BUDGET_GB) <= 1.5
    print(f"[verify] {'PASS' if ok else 'FAIL'}: 实占 {used_gb:.2f}GB {'≈' if ok else '≠'} 预算 {BUDGET_GB}GB")

    print("[verify] 卸载…")
    try:
        adapter.unload()
    except Exception as e:  # noqa: BLE001
        print(f"[verify] 卸载告警: {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
