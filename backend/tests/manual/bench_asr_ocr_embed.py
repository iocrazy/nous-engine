"""真机横评:MOSS ASR / Unlimited-OCR / WeMM-Embedding 在不同 GPU 上的速度与质量。

目的是回答「哪个模型该放哪张卡」——不是跑分,是**选型**。所以每个模型都用贴近真实
使用的负载,而不是合成 micro-benchmark:

  ASR   7.7 分钟真实播客(infra/moss-asr/logs/podcast_16k.wav,与 2026-07-20 SPIKE
        用的同一个文件,所以能跟那份 3090 历史数据严格对比)→ wall time 与实时倍率。
  OCR   一张**内容已知**的中英混排文档图(make_ocr_testdoc.py 生成)→ wall time、
        输出 token 速率,以及按字符编辑距离算的 CER(GT 是源码常量,不靠人工抄写)。
  嵌入  文本 + 图像各若干条 → 单条延迟与吞吐(embedding 是短请求,延迟比吞吐重要)。

**不测什么**:不测模型之间谁更准(那是 MMEB / 各自的 benchmark 的事),只测同一个
模型在不同卡上的表现差异,以及它在 24GiB 卡上装不装得下。

跑法(uv 不 load .env,先 source):
    cd backend && set -a && source .env && set +a && \
        uv run python tests/manual/bench_asr_ocr_embed.py [--only asr,ocr,embed]
没给 --key 时用 ADMIN_TOKEN 自己铸临时 key,跑完删。
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx

BASE = os.environ.get("NOUS_BASE_URL", "http://127.0.0.1:8000")
HERE = Path(__file__).resolve().parent
PODCAST = HERE.parents[1] / "infra" / "moss-asr" / "logs" / "podcast_16k.wav"
if not PODCAST.exists():  # backend/ 在仓库根下一层
    PODCAST = HERE.parents[2] / "infra" / "moss-asr" / "logs" / "podcast_16k.wav"
DOC = HERE / "_ocr_testdoc.png"

SERVICES = {"asr": "moss-asr", "ocr": "unlimited-ocr", "embed": "wemm-embedding-2b"}

EMBED_TEXTS = [
    "怎么做麻婆豆腐?",
    "麻婆豆腐的做法:嫩豆腐加豆瓣酱与花椒烧制。",
    "今天上海的天气预报是多云转晴。",
    "gpu_memory_utilization 是占整卡容量的比例,不是绝对值。",
    "两张 RTX 3090 之间有 NVLink,合计 48GB 可做张量并行。",
    "The quick brown fox jumps over the lazy dog.",
    "向量检索的召回率随索引维度下降而缓慢衰减。",
    "MOSS ASR 的模型权重只有 1.80 GB。",
]


def _levenshtein(a: str, b: str) -> int:
    """字符级编辑距离。717 字符规模下 O(n·m) 完全够用,不引第三方依赖。"""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _norm(s: str) -> str:
    """算 CER 前的归一化。

    ⚠️ 这里必须先剥掉**版面解析的结构标记**再比。Unlimited-OCR 的 `document parsing.`
    模式输出的不是纯文本,而是带版面标注的结构化结果:
        text [44, 198, 638, 270]本机三卡布局:GPU 0 与 ...
        table [47, 580, 728, 798]<table>模型权重(GiB)...</table>
    行首的 `<类型> [x1,y1,x2,y2]` 和 `<table>` 标签是**版面还原能力**,不是识别错误。
    不剥就把它们全算成插入错误 —— 2026-09-16 初版就是这么算出个 44.31% 的假 CER,
    剥干净后实测是 0.00%(522 字全对)。标点在中英混排下全角/半角会漂,一并归一化。
    """
    import re
    s = re.sub(r"^(header|title|text|table|figure|caption)\s*\[[0-9,\s]+\]", "", s, flags=re.M)
    s = s.replace("<table>", "").replace("</table>", "")
    return re.sub(r"[|\-=*#`_>\s，,。.:;：；()（）「」]+", "", s)


def _gpu_of(name: str, token: str) -> str:
    try:
        r = httpx.get(f"{BASE}/api/v1/engines", headers={"Authorization": f"Bearer {token}"},
                      timeout=15, proxy=None).json()
        rows = r if isinstance(r, list) else r.get("engines", [])
        for e in rows:
            if e.get("name") == name:
                g = e.get("loaded_gpu")
                return f"GPU {g}" if g is not None else "未加载"
    except Exception:  # noqa: BLE001
        pass
    return "?"


def _gpu_name(idx: str) -> str:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            i, n = line.split(",", 1)
            if f"GPU {i.strip()}" == idx:
                return n.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def bench_asr(key: str, token: str) -> dict:
    with wave.open(str(PODCAST)) as w:
        secs = w.getnframes() / w.getframerate()
    gpu = _gpu_of("moss_transcribe_diarize", token)
    print(f"\n【ASR】{SERVICES['asr']}  on {gpu} {_gpu_name(gpu)}")
    print(f"  素材: {PODCAST.name}  {secs:.1f}s")
    with httpx.Client(timeout=900, proxy=None) as c:
        t0 = time.perf_counter()
        r = c.post(f"{BASE}/v1/audio/transcriptions",
                   headers={"Authorization": f"Bearer {key}"},
                   files={"file": (PODCAST.name, PODCAST.read_bytes(), "audio/wav")},
                   data={"model": SERVICES["asr"], "response_format": "verbose_json",
                         "max_new_tokens": "16384"})
        wall = time.perf_counter() - t0
    if r.status_code != 200:
        print(f"  ❌ HTTP {r.status_code}: {r.text[:200]}")
        return {}
    d = r.json()
    segs = d.get("segments") or []
    chars = len(d.get("text") or "")
    print(f"  wall {wall:.2f}s   实时倍率 {secs/wall:.1f}×   段数 {len(segs)}   文本 {chars} 字")
    return {"model": "MOSS ASR", "gpu": gpu, "wall": wall, "rtf": secs / wall,
            "extra": f"{len(segs)} 段 / {chars} 字"}


def bench_ocr(key: str, token: str) -> dict:
    from make_ocr_testdoc import GROUND_TRUTH  # noqa: PLC0415

    gpu = _gpu_of("unlimited_ocr", token)
    print(f"\n【OCR】{SERVICES['ocr']}  on {gpu} {_gpu_name(gpu)}")
    b64 = "data:image/png;base64," + base64.b64encode(DOC.read_bytes()).decode()
    body = {
        "model": SERVICES["ocr"],
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": b64}},
            {"type": "text", "text": "document parsing."},
        ]}],
        "max_tokens": 4096, "temperature": 0.0,
    }
    with httpx.Client(timeout=900, proxy=None) as c:
        t0 = time.perf_counter()
        r = c.post(f"{BASE}/v1/chat/completions",
                   headers={"Authorization": f"Bearer {key}"}, json=body)
        wall = time.perf_counter() - t0
    if r.status_code != 200:
        print(f"  ❌ HTTP {r.status_code}: {r.text[:300]}")
        return {}
    d = r.json()
    text = d["choices"][0]["message"]["content"]
    usage = d.get("usage") or {}
    out_tok = usage.get("completion_tokens") or 0
    gt, hyp = _norm(GROUND_TRUTH), _norm(text)
    cer = _levenshtein(gt, hyp) / max(len(gt), 1)
    print(f"  wall {wall:.2f}s   输出 {out_tok} tok   {out_tok/wall:.1f} tok/s")
    print(f"  prompt {usage.get('prompt_tokens')} tok(含视觉 token)")
    print(f"  CER {cer*100:.2f}%   (归一化后 GT {len(gt)} 字 / 输出 {len(hyp)} 字)")
    (HERE / "_ocr_out.txt").write_text(text)
    print(f"  完整输出已存 {HERE/'_ocr_out.txt'}")
    return {"model": "Unlimited-OCR", "gpu": gpu, "wall": wall,
            "rtf": out_tok / wall if wall else 0, "extra": f"CER {cer*100:.2f}% / {out_tok} tok"}


def bench_embed(key: str, token: str) -> dict:
    gpu = _gpu_of("wemm_embedding_2b", token)
    print(f"\n【Embedding】{SERVICES['embed']}  on {gpu} {_gpu_name(gpu)}")
    imgs = sorted(Path("/media/heygo/cache/output/comfyui").glob("Krea2_000[12]*.png"))[:4]
    with httpx.Client(timeout=600, proxy=None) as c:
        def one(content: list[dict]) -> float:
            t0 = time.perf_counter()
            r = c.post(f"{BASE}/v1/embeddings", headers={"Authorization": f"Bearer {key}"},
                       json={"model": SERVICES["embed"],
                             "messages": [{"role": "user", "content": content}]})
            r.raise_for_status()
            return time.perf_counter() - t0

        one([{"type": "text", "text": "warmup"}])  # 预热,排除首次 kernel 编译
        t_lat = [one([{"type": "text", "text": t}]) for t in EMBED_TEXTS]
        i_lat = []
        for p in imgs:
            b64 = "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()
            i_lat.append(one([{"type": "image_url", "image_url": {"url": b64}}]))
    tm, im = sum(t_lat) / len(t_lat), (sum(i_lat) / len(i_lat) if i_lat else 0)
    print(f"  文本 {len(t_lat)} 条: 平均 {tm*1000:.0f} ms/条  → {1/tm:.1f} 条/s")
    if i_lat:
        print(f"  图像 {len(i_lat)} 张: 平均 {im*1000:.0f} ms/张 → {1/im:.2f} 张/s")
    return {"model": "WeMM-Embedding-2B", "gpu": gpu, "wall": tm,
            "rtf": 1 / tm, "extra": f"文本 {tm*1000:.0f}ms/条, 图像 {im*1000:.0f}ms/张"}


def _mint(admin: str, names: list[str]) -> tuple[str, str]:
    c = httpx.Client(timeout=30, proxy=None)
    all_svc = c.get(f"{BASE}/api/v1/services", headers={"Authorization": f"Bearer {admin}"}).json()
    rows = all_svc if isinstance(all_svc, list) else all_svc.get("items", [])
    ids = [s["id"] for s in rows if s["name"] in names]
    r = c.post(f"{BASE}/api/v1/keys", headers={"Authorization": f"Bearer {admin}"},
               json={"label": "tmp-bench-3way", "service_ids": ids})
    r.raise_for_status()
    d = r.json()
    c.close()
    return d["secret"], str(d["id"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="asr,ocr,embed")
    ap.add_argument("--key")
    args = ap.parse_args()
    want = [x.strip() for x in args.only.split(",") if x.strip()]

    admin = os.environ.get("ADMIN_TOKEN", "")
    key, kid = args.key, None
    if not key:
        if not admin:
            raise SystemExit("没 ADMIN_TOKEN(uv 不 load .env,先 source)")
        key, kid = _mint(admin, [SERVICES[w] for w in want if w in SERVICES])
        print(f"[bench] 临时 key {kid}(跑完删)")

    sys.path.insert(0, str(HERE))
    results = []
    try:
        for w in want:
            fn = {"asr": bench_asr, "ocr": bench_ocr, "embed": bench_embed}.get(w)
            if fn:
                try:
                    r = fn(key, admin)
                    if r:
                        results.append(r)
                except Exception as e:  # noqa: BLE001
                    print(f"  ❌ {w} 失败: {type(e).__name__}: {str(e)[:200]}")
    finally:
        if kid:
            httpx.Client(timeout=30, proxy=None).delete(
                f"{BASE}/api/v1/keys/{kid}", headers={"Authorization": f"Bearer {admin}"})
            print(f"\n[bench] 已删临时 key {kid}")

    print(f"\n{'='*78}\n### 汇总\n{'='*78}")
    print(f"{'模型':<22}{'落卡':<10}{'wall(s)':>10}   备注")
    for r in results:
        print(f"{r['model']:<22}{r['gpu']:<10}{r['wall']:>10.2f}   {r['extra']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
