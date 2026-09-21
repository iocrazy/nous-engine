"""量出各模型在 Pro 5000 上的真实显存地板(2026-09-20 两卡迁移 Task 3)。

地板 = 权重 + 峰值激活 + CUDAGraph(不含 KV)。它决定 util 的下限:低于它
vLLM 会报 "No available memory for the cache blocks" 直接起不来。
稳态占用则是它实际长期吃掉的量,决定几个模型能不能共存。

只加载不推理,逐个来(共存测试在 Task 4)。每个测完立即卸载。
"""
import asyncio, os, subprocess, sys, time
os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import yaml
from src.services.inference.llm_vllm import VLLMAdapter

DEVICE = "cuda:0"          # Pro 5000
CARD_GIB = 71.7

KEYS = ("KV cache", "model weights", "memory profiling", "GPU KV cache size",
        "Maximum concurrency", "non-torch memory", "peak activation", "Free memory on device")


def gpu_used_mib(uuid_frag="f4334111"):
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory,gpu_uuid",
                          "--format=csv,noheader"], capture_output=True, text=True).stdout
    return sum(int(l.split(",")[1].strip().split()[0]) for l in out.splitlines() if uuid_frag in l)


async def measure(model_id: str, util: float):
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "configs" / "models.d" / f"{model_id}.yaml").read_text())
    p = cfg.get("params", {})
    base = gpu_used_mib()
    ad = VLLMAdapter(
        paths=cfg["paths"], vllm_runner=p.get("vllm_runner"),
        max_model_len=p.get("max_model_len"), max_num_seqs=p.get("max_num_seqs"),
        gpu_memory_utilization=util, vllm_args=p.get("vllm_args"),
        quantization=p.get("quantization"),
    )
    print(f"\n{'='*70}\n### {model_id}  util={util}  ({util*CARD_GIB:.1f} GiB 预算)\n{'='*70}")
    t0 = time.perf_counter()
    try:
        await ad.load(device=DEVICE)
    except Exception as e:
        print(f"  ❌ 起不来: {type(e).__name__}: {str(e)[:200]}")
        for ln in list(getattr(ad, "_stdout_tail", []))[-25:]:
            if any(k.lower() in ln.lower() for k in ("memory", "cache blocks", "error")):
                print("   ", ln.strip())
        return
    print(f"  加载 {time.perf_counter()-t0:.1f}s")
    for ln in list(getattr(ad, "_stdout_tail", [])):
        if any(k.lower() in ln.lower() for k in KEYS):
            print("   ", ln.strip())
    time.sleep(3)
    print(f"  nvidia-smi 稳态增量: {gpu_used_mib()-base} MiB")
    ad.unload()
    time.sleep(5)


async def main():
    for mid, util in [(a.split("=")[0], float(a.split("=")[1])) for a in sys.argv[1:]]:
        await measure(mid, util)

if __name__ == "__main__":
    asyncio.run(main())
