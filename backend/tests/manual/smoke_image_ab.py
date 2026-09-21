"""引擎正确性 smoke —— modular 出图的 golden 回归比对。standalone,需 GPU。

历史:本来是 legacy(自写 ImageSampler)vs modular(Modular Diffusers)的 A/B(D6 查
6.4s vs 27s)。**legacy 引擎已在 #128-132 删除**,现在只剩 modular 一套。所以这脚本现
在的用途 = 跑当前 modular 出图,跟保存的 golden 图做 SSIM(≥0.97 即引擎无回归),作为改
`image_modular.py` / 升 diffusers 前的正确性闸门(CLAUDE.md「图像引擎」)。

用法(落 Pro 6000;真模型 1024/20step ~6.5s infer):
    cd backend
    SMOKE_ENGINE=modular SMOKE_DEVICE=cuda:1 uv run python tests/manual/smoke_image_ab.py
    # 出图存 _smoke_out/ab_modular.png(先把已知好的那张备份成 golden 再比)
    SMOKE_SSIM=1 uv run python tests/manual/smoke_image_ab.py   # 比 ab_legacy.png vs ab_modular.png

注:`_smoke_out/` 在 .gitignore 里,golden 只在本地。改 image_modular 前先 cp 一份当前好图
当 golden,改后重生成再 SSIM 对比。
"""
from __future__ import annotations

import os

# standalone 必须在 import torch 前固定 PCI_BUS_ID —— 否则 torch 默认 FASTEST_FIRST 按
# 算力排序,两张卡**对调**:cuda:0 成了 Pro 6000、cuda:1 成了 Pro 5000。于是下面默认的
# SMOKE_DEVICE=cuda:1 本意是 ComfyUI 那张空闲的 Pro 6000,实际落到跑着全部常驻模型的
# Pro 5000 上(常驻已占 ~58GiB)—— OOM,还会把推理服务一起拖下水。
# (2026-09-20 两卡改造前这里的形状是 cuda:1 变成 24G 的 3090;卡换了,坑还在。)
# 生产走 src/api/main.py 已 setdefault,但本脚本不经它、且 uv 不 load .env,得自己设。
os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")

import asyncio
import glob
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MODEL_ROOT = Path(os.environ.get(
    "SMOKE_MODEL", "/media/heygo/Program/models/nous/media/diffusers/Flux2-klein-9B"))
DEVICE = os.environ.get("SMOKE_DEVICE", "cuda:1")
DTYPE = os.environ.get("SMOKE_DTYPE", "bfloat16")  # 强制 bf16(对齐 spike,避免 default→fp32 OOM)
ENGINE = os.environ.get("SMOKE_ENGINE", "modular")  # legacy 已删,只剩 modular
PROMPT = os.environ.get("SMOKE_PROMPT", "a photo of a red fox sitting in autumn leaves, sharp focus, detailed")
OUT_DIR = Path(__file__).parent / "_smoke_out"
SEED, STEPS, SIZE = 42, 20, 1024


def _rep(d: Path) -> str:
    hits = sorted(glob.glob(str(d / "*.safetensors")))
    if not hits:
        raise SystemExit(f"组件目录无 .safetensors: {d}")
    return hits[0]


def _ssim(p1: Path, p2: Path) -> float | None:
    try:
        import numpy as np
        from PIL import Image
        from skimage.metrics import structural_similarity as ssim
    except Exception as e:  # noqa: BLE001
        print(f"[ab] SSIM 跳过(缺 skimage/PIL: {e})")
        return None
    a = np.asarray(Image.open(p1).convert("L"))
    b = np.asarray(Image.open(p2).convert("L"))
    return float(ssim(a, b))


async def _run(mm, components, engine: str) -> tuple[float, float, Path]:
    from src.services.inference.base import ImageRequest

    os.environ["NOUS_IMAGE_ENGINE"] = engine
    t0 = time.monotonic()
    adapter = await mm.get_or_load_image_adapter(components, "Flux2KleinPipeline")
    load_s = time.monotonic() - t0

    req = ImageRequest(
        request_id=f"ab-{engine}", prompt=PROMPT, negative_prompt="",
        width=SIZE, height=SIZE, steps=STEPS, cfg_scale=4.0, seed=SEED,
        components=components, pipeline_class="Flux2KleinPipeline",
    )
    t1 = time.monotonic()
    res = await adapter.infer(req)
    infer_s = time.monotonic() - t1

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"ab_{engine}.png"
    out.write_bytes(res.data)
    print(f"[ab:{engine}] load {load_s:.1f}s | infer {infer_s:.1f}s → {out}")
    return load_s, infer_s, out


async def main() -> None:
    # SSIM-only 模式:比已存的两图,不加载模型
    if os.environ.get("SMOKE_SSIM"):
        a, b = OUT_DIR / "ab_legacy.png", OUT_DIR / "ab_modular.png"
        if not (a.exists() and b.exists()):
            raise SystemExit(f"缺 {a} 或 {b}(先各跑一次 legacy/modular)")
        s = _ssim(a, b)
        print(f"[ab] SSIM(legacy, modular) = {s:.4f}" if s is not None else "[ab] SSIM n/a")
        return

    from src.services.gpu_allocator import GPUAllocator
    from src.services.inference.component_spec import ComponentSpec
    from src.services.inference.registry import ModelRegistry
    from src.services.model_manager import ModelManager

    class _EmptyRegistry(ModelRegistry):
        def __init__(self):
            self._config_path = ""
            self._specs = {}

    mm = ModelManager(registry=_EmptyRegistry(), allocator=GPUAllocator())
    components = {
        "diffusion_models": ComponentSpec(kind="diffusion_models", file=_rep(MODEL_ROOT / "transformer"),
                              device=DEVICE, dtype=DTYPE, adapter_arch="flux2", loras=[]),
        "clip": ComponentSpec(kind="clip", file=_rep(MODEL_ROOT / "text_encoder"), device=DEVICE, dtype=DTYPE),
        "vae":  ComponentSpec(kind="vae", file=_rep(MODEL_ROOT / "vae"), device=DEVICE, dtype=DTYPE),
    }
    print(f"[ab] engine={ENGINE} device={DEVICE} dtype={DTYPE} seed={SEED} steps={STEPS} size={SIZE}")
    await _run(mm, components, ENGINE)
    print("[ab] OK")


if __name__ == "__main__":
    asyncio.run(main())
