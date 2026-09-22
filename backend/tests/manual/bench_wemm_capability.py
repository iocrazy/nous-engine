"""真机能力测评:WeMM-Embedding 各档的跨模态检索 / 细粒度判别 / MRL 截断(2026-09-15)。

跟 `verify_wemm_embedding.py` 的分工:那个是**冒烟**(配置能不能起、向量有没有意义,
一个同义三元组就够);这个是**选型**(2B / 4B / 9B 到底差在哪,值不值得多花显存)。
两个都进不了 CI —— conftest 的 Popen 护栏起不了真 vLLM,而且 CI 无 GPU。

测四件事:
  1. **图 → 文检索**:每张图对全部候选描述算相似度,正确描述该排第 1。
  2. **文 → 图检索**:反向,每条描述对全部图排序。
  3. **难例判别**:图 A/B 都是「棕发 + 白围巾 + 白外套的动漫少女」,只有裙色和背景不同
     (A = 白底四视图设定 + 粉裙,B = 黄昏雪原 + 红裙)。粗粒度模型会把两者混掉 ——
     这是 2B/4B/9B 拉开差距最可能的地方,也是实际建库最容易踩的坑(同一角色的不同
     设定图互相召回)。
  4. **MRL 截断**:官方 README 说 256 维保留 98.7% 的图像/视频效果。这里直接验:
     截到 64/128/256/512/1024 后 Top-1 还对不对、与满维的排序一致性(Spearman)。
     索引小 8 倍如果真只掉 1.3%,对向量库的意义比省那几 G 显存大得多。

**必须走 messages**:WeMM 的 embedding_chat_template.jinja 在末尾追加 `<embedding>`,
而 vLLM 的 /v1/embeddings 只有带 messages 时才套模板(CLAUDE.md「Embedding 模型」)。
传 `input` 字符串是裸 tokenize,算出来的向量和这里的结论都对不上。

跑法(经 nous 的 /v1/embeddings,需要模型已加载 + 一把授权给该服务的 key):
    cd backend && set -a && source .env && set +a && \
        uv run python tests/manual/bench_wemm_capability.py --key <secret> \
            [--services wemm-embedding-2b,wemm-embedding-4b]
没给 --key 时脚本用 ADMIN_TOKEN 自己铸一把临时 key,跑完删掉。
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import sys
from pathlib import Path

import httpx

BASE = os.environ.get("NOUS_BASE_URL", "http://127.0.0.1:8000")
OUT = Path("/media/heygo/cache/output/comfyui")

# 四张真实出图 + 人工核对过的描述(ground truth 是我逐张看图写的,不是文件名猜的)。
# A/B 故意选成难例对:同为棕发 + 白围巾 + 白外套的动漫少女。
CASES = [
    {
        "id": "A",
        "path": OUT / "Krea2_00027_.png",
        "caption": "动漫风格的角色设定图,棕色长发少女戴白色围巾穿白色外套配粉色裙子,"
                   "纯白背景上并排画出正面、侧面、背面全身三视图和一张面部特写",
        "hard_query": "白色背景上的角色三视图设定稿",
    },
    {
        "id": "B",
        "path": OUT / "818d4014_000.png",
        "caption": "动漫风格的单人半身立绘,棕色长发少女戴白色围巾穿白色外套配红色裙子,"
                   "背景是黄昏时分的雪原和远处的树林",
        "hard_query": "黄昏雪原里的少女",
    },
    {
        "id": "C",
        "path": OUT / "AB_zimg_A_stock_00001_.png",
        "caption": "写实照片风格,黑长发的亚洲女性穿黑白蕾丝女仆裙戴蕾丝头饰,"
                   "坐在洒进阳光的窗边,背景有木桌和一只白色茶杯",
        "hard_query": "窗边阳光下穿蕾丝裙的女性照片",
    },
    {
        "id": "D",
        "path": OUT / "ComfyUI_00003_.png",
        "caption": "两名赤膊的男子在草地上戴着红色和蓝色拳击手套对打,背景是白色围栏和树木",
        "hard_query": "户外草地上的拳击对练",
    },
]

MRL_DIMS = [64, 128, 256, 512, 1024]


def _b64_image(path: Path, max_side: int = 768) -> str:
    """缩到 max_side 再转 data URI。

    不缩的话 1920×1080 那张过 ViT 会吃掉大量 token,可能顶破 max_model_len: 8192;
    而且 embedding 关心的是语义不是像素,缩图不影响结论、还快不少。
    """
    from PIL import Image

    im = Image.open(path).convert("RGB")
    if max(im.size) > max_side:
        ratio = max_side / max(im.size)
        im = im.resize((int(im.width * ratio), int(im.height * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _truncate(v: list[float], d: int) -> list[float]:
    """MRL 截断 —— 取前 d 维。Matryoshka 的前缀本身就是一个可用的低维表示,
    不需要重新归一化(余弦对模长不敏感)。"""
    return v[:d]


def _spearman(x: list[float], y: list[float]) -> float:
    """排序一致性 —— 样本量只有 4,用秩相关比看绝对分数更有意义。"""
    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(order):
            r[i] = float(pos)
        return r
    rx, ry = ranks(x), ranks(y)
    n = len(x)
    d2 = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return 1 - 6 * d2 / (n * (n * n - 1)) if n > 1 else 1.0


class Client:
    def __init__(self, key: str, service: str):
        self._c = httpx.Client(timeout=180, proxy=None)
        self._key = key
        self._svc = service

    def embed(self, content: list[dict]) -> list[float]:
        r = self._c.post(
            f"{BASE}/v1/embeddings",
            headers={"Authorization": f"Bearer {self._key}"},
            json={"model": self._svc, "messages": [{"role": "user", "content": content}]},
        )
        if r.status_code != 200:
            raise RuntimeError(f"{self._svc} HTTP {r.status_code}: {r.text[:300]}")
        return r.json()["data"][0]["embedding"]

    def text(self, t: str) -> list[float]:
        return self.embed([{"type": "text", "text": t}])

    def image(self, path: Path) -> list[float]:
        return self.embed([{"type": "image_url", "image_url": {"url": _b64_image(path)}}])

    def close(self) -> None:
        self._c.close()


def run_service(service: str, key: str) -> dict:
    cli = Client(key, service)
    print(f"\n{'=' * 74}\n### {service}\n{'=' * 74}")
    try:
        img_vecs, cap_vecs, hq_vecs = {}, {}, {}
        for c in CASES:
            if not c["path"].exists():
                raise SystemExit(f"素材不存在: {c['path']}")
            img_vecs[c["id"]] = cli.image(c["path"])
            cap_vecs[c["id"]] = cli.text(c["caption"])
            hq_vecs[c["id"]] = cli.text(c["hard_query"])
        dim = len(img_vecs["A"])
        print(f"维度 = {dim}")

        ids = [c["id"] for c in CASES]

        # ---- 1. 图 → 文 -------------------------------------------------
        print("\n【1】图 → 文检索(行=图,列=描述;对角线该最大)")
        print("        " + "".join(f"{j:>9}" for j in ids))
        i2t_hit = 0
        for i in ids:
            sims = [_cos(img_vecs[i], cap_vecs[j]) for j in ids]
            best = ids[max(range(len(ids)), key=lambda k: sims[k])]
            i2t_hit += best == i
            mark = "✓" if best == i else f"✗→{best}"
            print(f"  图{i}  " + "".join(f"{s:>9.4f}" for s in sims) + f"   {mark}")
        print(f"  Top-1 命中 {i2t_hit}/{len(ids)}")

        # ---- 2. 文 → 图 -------------------------------------------------
        print("\n【2】文 → 图检索(行=描述,列=图)")
        t2i_hit = 0
        for i in ids:
            sims = [_cos(cap_vecs[i], img_vecs[j]) for j in ids]
            best = ids[max(range(len(ids)), key=lambda k: sims[k])]
            t2i_hit += best == i
            mark = "✓" if best == i else f"✗→{best}"
            print(f"  文{i}  " + "".join(f"{s:>9.4f}" for s in sims) + f"   {mark}")
        print(f"  Top-1 命中 {t2i_hit}/{len(ids)}")

        # ---- 3. 难例 A vs B --------------------------------------------
        print("\n【3】难例判别(A/B 同为棕发+白围巾+白外套的动漫少女)")
        hard_ok = 0
        for c in CASES:
            i = c["id"]
            sims = {j: _cos(hq_vecs[i], img_vecs[j]) for j in ids}
            best = max(sims, key=sims.get)
            hard_ok += best == i
            ab = f"A={sims['A']:.4f} B={sims['B']:.4f}"
            mark = "✓" if best == i else f"✗→{best}"
            print(f"  「{c['hard_query']}」→ {best} {mark}   ({ab}, 差 {abs(sims['A']-sims['B']):.4f})")
        print(f"  命中 {hard_ok}/{len(ids)}")

        # ---- 4. MRL 截断 -----------------------------------------------
        print("\n【4】MRL 截断(基线=满维的图→文 Top-1 与排序)")
        full_rows = {i: [_cos(img_vecs[i], cap_vecs[j]) for j in ids] for i in ids}
        print(f"  {'维度':>6}  {'图→文 Top-1':>12}  {'排序一致性(Spearman)':>22}")
        mrl = {}
        for d in [*MRL_DIMS, dim]:
            if d > dim:
                continue
            hit, rhos = 0, []
            for i in ids:
                row = [_cos(_truncate(img_vecs[i], d), _truncate(cap_vecs[j], d)) for j in ids]
                if ids[max(range(len(ids)), key=lambda k: row[k])] == i:
                    hit += 1
                rhos.append(_spearman(row, full_rows[i]))
            rho = sum(rhos) / len(rhos)
            mrl[d] = (hit, rho)
            tag = "  ← 满维" if d == dim else ""
            print(f"  {d:>6}  {f'{hit}/{len(ids)}':>12}  {rho:>22.3f}{tag}")

        return {"service": service, "dim": dim, "i2t": i2t_hit, "t2i": t2i_hit,
                "hard": hard_ok, "n": len(ids), "mrl": mrl}
    finally:
        cli.close()


def _mint_key(admin: str, services: list[str]) -> tuple[str, str]:
    c = httpx.Client(timeout=30, proxy=None)
    all_svc = c.get(f"{BASE}/api/v1/services",
                    headers={"Authorization": f"Bearer {admin}"}).json()
    rows = all_svc if isinstance(all_svc, list) else all_svc.get("items", [])
    ids = [s["id"] for s in rows if s["name"] in services]
    if len(ids) != len(services):
        raise SystemExit(f"服务没找齐: 要 {services},库里匹配到 {len(ids)} 个")
    r = c.post(f"{BASE}/api/v1/keys", headers={"Authorization": f"Bearer {admin}"},
               json={"label": "tmp-wemm-bench", "service_ids": ids})
    r.raise_for_status()
    d = r.json()
    c.close()
    return d["secret"], str(d["id"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", help="InstanceApiKey secret;不给就用 ADMIN_TOKEN 铸临时 key")
    ap.add_argument("--services", default="wemm-embedding-2b,wemm-embedding-4b")
    args = ap.parse_args()

    services = [s.strip() for s in args.services.split(",") if s.strip()]
    key, kid, admin = args.key, None, os.environ.get("ADMIN_TOKEN", "")
    if not key:
        if not admin:
            raise SystemExit("既没 --key 也没 ADMIN_TOKEN(uv 不 load .env,先 source)")
        key, kid = _mint_key(admin, services)
        print(f"[bench] 铸了临时 key {kid}(跑完删)")

    try:
        results = [run_service(s, key) for s in services]
    finally:
        if kid:
            httpx.Client(timeout=30, proxy=None).delete(
                f"{BASE}/api/v1/keys/{kid}", headers={"Authorization": f"Bearer {admin}"})
            print(f"\n[bench] 已删临时 key {kid}")

    print(f"\n{'=' * 74}\n### 汇总\n{'=' * 74}")
    print(f"{'服务':<22}{'维度':>6}{'图→文':>8}{'文→图':>8}{'难例':>8}   MRL-256 Top-1")
    for r in results:
        n = r["n"]
        m256 = r["mrl"].get(256)
        s256 = f"{m256[0]}/{n} (ρ={m256[1]:.2f})" if m256 else "—"
        i2t, t2i, hard = f"{r['i2t']}/{n}", f"{r['t2i']}/{n}", f"{r['hard']}/{n}"
        print(f"{r['service']:<22}{r['dim']:>6}{i2t:>8}{t2i:>8}{hard:>8}   {s256}")
    print("\n注:4 个样本的检索命中率是**定性信号不是基准分**(MMEB-v2 才是)。"
          "看点在难例 A/B 的相似度差和 MRL 截断后的排序一致性。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
