"""/v1/rerank 真机验证(非 CI):文本区分度 + 图文重排。

前提:含 /v1/rerank 的后端已上线,目标 rerank 服务的模型已加载(2B 常驻;8B 按需,先在模型页加载)。
鉴权:backend/.env 的 ADMIN_TOKEN(bearer 旁路);或 --api-key 用一把真实 key 连 grant/配额一起验。

    cd backend && uv run python tests/manual/verify_rerank.py \
        [--base http://127.0.0.1:8000] [--service nous-qwen3-vl-reranker-2b] \
        [--api-key sk-...] [--env-file ../backend/.env] [--image ./cat.jpg]

判据:相关的排第一、无关的排最后(文本);给 --image 时,图文 query 下含猫的描述排第一。
2B 与 8B 的分数尺度不同(2026-09-27 同一组相关/部分相关/无关:2B 0.896/0.626/0.426,
8B 0.271/0.165/0.029),只比同一模型内部的排序,别跨模型比绝对值、也别拿一个阈值套两档。
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import sys
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE.parents[1] / ".env"
QUERY = "橘猫在窗台上晒太阳"
DOCS = ["一只橘色的猫趴在阳光照射的窗台上打盹", "一只黑猫在屋顶上", "今天 A 股三大指数集体收跌"]


def _admin_token(env_file: Path) -> str:
    if not env_file.is_file():
        sys.exit(f"[FAIL] 找不到 {env_file}(worktree 里没有 .env;用 --env-file 指向生产检出的 backend/.env)")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit(f"[FAIL] {env_file} 里没有 ADMIN_TOKEN")


def _rerank(client: httpx.Client, body: dict[str, Any]) -> list[dict[str, Any]]:
    r = client.post("/v1/rerank", json=body)
    if r.status_code >= 400:
        sys.exit(f"[FAIL] /v1/rerank: HTTP {r.status_code} {r.text[:800]}")
    data = r.json()
    print(f"  usage={data.get('usage')}")
    return data["results"]


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--service", default="nous-qwen3-vl-reranker-2b")
    ap.add_argument("--api-key")
    ap.add_argument("--env-file", type=Path, default=ENV_FILE)
    ap.add_argument("--image", type=Path, help="一张含猫的图,验证图文重排")
    args = ap.parse_args()

    token = args.api_key or _admin_token(args.env_file)
    with httpx.Client(base_url=args.base, headers={"Authorization": f"Bearer {token}"}, timeout=120) as c:
        print(f"[text] {args.service}")
        results = _rerank(c, {"model": args.service, "query": QUERY, "documents": DOCS})
        for x in results:
            print(f"  {x['relevance_score']:.4f}  #{x['index']} {DOCS[x['index']]}")
        order = [x["index"] for x in results]
        if order[0] != 0 or order[-1] != 2:
            sys.exit(f"[FAIL] 文本排序 {order}:相关的应排第一、无关的应排最后")

        if args.image:
            print("[image] query=文本,documents=[图, 无关文本]")
            docs = [{"content": [{"type": "image_url", "image_url": {"url": _data_url(args.image)}}]},
                    "今天 A 股三大指数集体收跌"]
            results = _rerank(c, {"model": args.service, "query": "一只猫", "documents": docs})
            for x in results:
                print(f"  {x['relevance_score']:.4f}  #{x['index']}")
            if results[0]["index"] != 0:
                sys.exit("[FAIL] 图文重排:图片没有排第一")
    print("[OK]")


if __name__ == "__main__":
    main()
