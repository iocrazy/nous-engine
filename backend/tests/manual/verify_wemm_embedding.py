"""真机验证:WeMM-Embedding 经 vLLM pooling 起服务并算出可用的向量(2026-09-11 接入)。

CI 跑不了这个(conftest 有 Popen 护栏,禁止真起 vLLM;而且 CI 无 GPU),所以 WeMM 的
「配置是否真能起、算出来的向量是否有意义」只靠这个 standalone 脚本兜。

验三件事:
  1. 按 configs/models.d/wemm_embedding_4b.yaml 的**真实配置**(含 `{model_dir}`
     展开出的 --chat-template)能起来。
  2. 语义合理:同义句余弦 > 无关句余弦(对齐 test_embeddings_endpoint 里记的
     Qwen3-Embedding-4B 基线 0.72 > 0.41)。
  3. `input:` 纯文本路径 vs `messages:` 路径算出的向量差多少 —— WeMM 的
     embedding_chat_template.jinja 会在末尾追加 `<embedding>` token,而 vLLM 的
     /v1/embeddings 只有走 messages 才套模板。两条路差得大的话,调用方就必须走
     messages,不能图省事传 input 字符串。

CUDA_DEVICE_ORDER=PCI_BUS_ID 必须在 import torch 前设(CLAUDE.md):否则 torch 默认
FASTEST_FIRST 会把 Pro 6000 排成 cuda:0,cuda:1 变成 24G 的 3090,9B 直接 OOM。

跑法(uv 不 load .env,先 source):
    cd backend && set -a && source .env && set +a && \
        uv run python tests/manual/verify_wemm_embedding.py [4b|9b]
"""
import os

os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")

import asyncio
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/ 进 sys.path

import httpx
import yaml

from src.services.inference.llm_vllm import VLLMAdapter

DEVICE = "cuda:1"  # Pro6000 96G(PCI_BUS_ID 序)

# 同义 / 无关三元组:q 与 pos 说的是一回事,与 neg 无关。
QUERY = "怎么做麻婆豆腐?"
POSITIVE = "麻婆豆腐的做法:嫩豆腐加豆瓣酱与花椒烧制。"
NEGATIVE = "今天上海的天气预报是多云转晴。"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _load_cfg(which: str) -> dict:
    """直接读 models.d 的 yaml —— 验的是**线上那份配置**,不是脚本里另抄一份。"""
    p = Path(__file__).resolve().parents[2] / "configs" / "models.d" / f"wemm_embedding_{which}.yaml"
    return yaml.safe_load(p.read_text())


async def _embed_input(client: httpx.AsyncClient, base: str, texts: list[str]) -> list[list[float]]:
    r = await client.post(f"{base}/v1/embeddings", json={"model": "", "input": texts})
    r.raise_for_status()
    return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


async def _embed_messages(client: httpx.AsyncClient, base: str, texts: list[str]) -> list[list[float]]:
    """走 messages 形式 —— 这条才过 embedding_chat_template.jinja(追加 <embedding>)。"""
    out = []
    for t in texts:
        r = await client.post(
            f"{base}/v1/embeddings",
            json={"model": "", "messages": [{"role": "user", "content": [{"type": "text", "text": t}]}]},
        )
        r.raise_for_status()
        out.append(r.json()["data"][0]["embedding"])
    return out


def _print_memory_profile(adapter: VLLMAdapter, util: float | None) -> None:
    """把 vLLM 启动日志里的显存标定行捞出来打印。

    这三个 yaml 的 `gpu_memory_utilization` 是**实测标定**的(CLAUDE.md:别按权重大小
    猜 —— 视觉塔带多模态 dummy 输入的 profiling 峰值才是大头),而标定要看的就是
    "KV cache memory" / "model weights" 这几行。适配器把子进程 stdout 收在有界 deque
    里(`_stdout_tail`,防 PIPE 填满死锁),不落盘也不外打 —— 不捞出来的话,调 util 的
    人只能靠 nvidia-smi 瞎猜,或者把 util 调到起不来才知道下限在哪。
    """
    tail = list(getattr(adapter, "_stdout_tail", []) or [])
    keys = ("KV cache", "model weights", "memory profiling", "GPU KV cache size",
            "Maximum concurrency", "non-torch memory", "torch peak")
    hits = [ln.rstrip() for ln in tail if any(k.lower() in ln.lower() for k in keys)]
    if not hits:
        print("[verify] (没在 stdout tail 里捞到显存标定行 —— deque 只留最后 200 行,"
              "可能已被推出;要精确数字就临时调大 _stdout_tail 的 maxlen)")
        return
    print(f"[verify] ── 显存标定(gpu_memory_utilization={util})──")
    for ln in hits:
        print(f"[verify]   {ln.strip()}")


async def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "4b").lower()
    cfg = _load_cfg(which)
    params = cfg.get("params", {})
    print(f"[verify] {cfg['id']}  path={cfg['paths']['main']}  gpu={cfg.get('gpu')}")
    print(f"[verify] vllm_args={params.get('vllm_args')}")

    adapter = VLLMAdapter(
        paths=cfg["paths"],
        vllm_runner=params.get("vllm_runner"),
        max_model_len=params.get("max_model_len"),
        gpu_memory_utilization=params.get("gpu_memory_utilization"),
        vllm_args=params.get("vllm_args"),
    )
    print(f"[verify] 加载到 {DEVICE}(首次要编 kernel,最多 ~10 分钟)…")
    await adapter.load(device=DEVICE)
    base = adapter.base_url
    print(f"[verify] vLLM 就绪 base_url={base}")
    _print_memory_profile(adapter, params.get("gpu_memory_utilization"))

    rc = 1
    try:
        texts = [QUERY, POSITIVE, NEGATIVE]
        async with httpx.AsyncClient(timeout=120) as client:
            vi = await _embed_input(client, base, texts)
            print(f"[verify] input 路径:维度={len(vi[0])}")
            pos_i, neg_i = _cosine(vi[0], vi[1]), _cosine(vi[0], vi[2])
            print(f"[verify]   同义 {pos_i:.4f}  vs  无关 {neg_i:.4f}")

            try:
                vm = await _embed_messages(client, base, texts)
                pos_m, neg_m = _cosine(vm[0], vm[1]), _cosine(vm[0], vm[2])
                print(f"[verify] messages 路径(过 chat template):维度={len(vm[0])}")
                print(f"[verify]   同义 {pos_m:.4f}  vs  无关 {neg_m:.4f}")
                drift = _cosine(vi[0], vm[0])
                print(f"[verify] 两条路同一句的余弦 = {drift:.4f}"
                      f"  ({'基本一致' if drift > 0.99 else '**明显不同 —— 调用方必须走 messages**'})")
            except httpx.HTTPStatusError as e:
                print(f"[verify] messages 路径不可用({e.response.status_code}):"
                      f"{e.response.text[:200]}")
                pos_m = neg_m = None

            ok = pos_i > neg_i and (pos_m is None or pos_m > neg_m)
            print(f"[verify] {'PASS' if ok else 'FAIL'}: 同义余弦应显著高于无关余弦")
            rc = 0 if ok else 1
    finally:
        print("[verify] 卸载…")
        try:
            adapter.unload()
        except Exception as e:  # noqa: BLE001
            print(f"[verify] 卸载告警: {e}")
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
