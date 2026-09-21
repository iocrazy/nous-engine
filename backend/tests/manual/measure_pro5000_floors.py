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
# 只用来在标题里给个**粗略**预算。别引用这行打印出来的数字做判定:开机可见显存
# (vLLM 日志里的 71.12 GiB)比 nvidia-smi 的 total 小,util 越大偏差越大(实测
# 0.08~0.2 GiB)。真值取 vLLM 自己那行 "Desired GPU memory utilization"。
CARD_GIB = 71.7

KEYS = ("KV cache", "model weights", "memory profiling", "GPU KV cache size",
        "Maximum concurrency", "non-torch memory", "peak activation", "Free memory on device")


def gpu_used_mib(uuid_frag="f4334111"):
    """按 UUID 片段汇总某张卡上所有进程的显存。默认值是 Pro 5000 的 UUID 片段。

    先确认这块 UUID 真在机器上 —— 否则「没有匹配行」与「卡上没进程」都返回 0,
    换卡/重装驱动后会静默给出全错的增量,而不是报错。
    """
    listing = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True).stdout
    if uuid_frag not in listing:
        raise RuntimeError(f"nvidia-smi -L 里没有 UUID 片段 {uuid_frag};卡换了?\n{listing}")
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
        # 失败路径**不过滤、不截尾**:2026-09-20 qwen3.8 @util=0.34 起不来时,按关键词
        # 只看最后 25 行,捞到的是外层 `RuntimeError: ...See root cause above`(空摘要),
        # 真正带数字的 EngineCore ValueError(KV 差 0.44 GiB)早被挤出窗口,只好另跑一次
        # 不限长度的诊断日志才拿到根因。_stdout_tail 本身是 200 行有界 deque —— 真根因
        # 还被挤掉的话,得去调大适配器那个 deque,不是在这里加关键词。
        print(f"  ❌ 起不来: {type(e).__name__}: {str(e)[:200]}")
        print("  --- 子进程 stdout 尾部(全量,未过滤)---")
        for ln in list(getattr(ad, "_stdout_tail", [])):
            print("   ", ln.rstrip())
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
