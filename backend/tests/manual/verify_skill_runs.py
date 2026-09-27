"""skill-runs 真机端到端(非 CI):Qwen Image 2.1 文生图 + 图像编辑。

前提:含 /v1/skill-runs 的后端已上线;Chat 模型已加载;ComfyUI 在线且没有渲染在跑。
鉴权:backend/.env 的 ADMIN_TOKEN(bearer 旁路)。

    cd backend && uv run python tests/manual/verify_skill_runs.py \
        [--base http://127.0.0.1:8000] [--model nous-qwen3-8-27b-twolven] \
        [--t2i-service nous-qwen21-text-to-image] [--edit-service nous-qwen21-image-edit] \
        [--out ./skill_runs_out]

服务名会随控制面改名(2026-09-26 统一加了 nous- 前缀),以 /v1/models?include_unready=1 为准。

流程:preview(t2i Skill)→ generate <t2i-service> → 下载图;
      preview(edit Skill + 上一步的图)→ generate <edit-service>(input.image=同一张图)→ 下载图。
"""
from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE.parents[1] / ".env"
POLL_INTERVAL_S = 3.0
POLL_TIMEOUT_S = 15 * 60
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
# 合法值取自 ComfyUI ResolutionSelector 节点的选项(服务 schema 不暴露 enum)。
ASPECT_RATIO = "1:1 (Square)"


def _admin_token(env_file: Path) -> str:
    if not env_file.is_file():
        sys.exit(f"[FAIL] 找不到 {env_file}(worktree 里没有 .env;用 --env-file 指向生产检出的 backend/.env)")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit(f"[FAIL] {env_file} 里没有 ADMIN_TOKEN")


def _check(r: httpx.Response, what: str) -> dict[str, Any]:
    if r.status_code >= 400:
        sys.exit(f"[FAIL] {what}: HTTP {r.status_code} {r.text[:800]}")
    return r.json()


def _image_urls(output: Any) -> list[str]:
    """递归找产物里的图片 URL(桥节点产物是 {"items":[{"url","kind","filename"}]})。"""
    found: list[str] = []
    if isinstance(output, dict):
        url = output.get("url")
        name = str(output.get("filename") or url or "").lower()
        if isinstance(url, str) and (output.get("kind") == "image" or name.endswith(IMAGE_EXTS)):
            found.append(url)
        for v in output.values():
            found.extend(_image_urls(v))
    elif isinstance(output, list):
        for v in output:
            found.extend(_image_urls(v))
    return list(dict.fromkeys(found))


def _preview(c: httpx.Client, model: str, skill: str, user_input: Any) -> str:
    t0 = time.monotonic()
    out = _check(c.post("/v1/skill-runs/preview", json={
        "model": model,
        "skill": {"content": (HERE / "skill_runs" / skill).read_text(encoding="utf-8")},
        "input": user_input,
    }), f"preview {skill}")
    print(f"[preview] {skill} {time.monotonic() - t0:.1f}s usage={out['usage']}\n  → {out['text']}")
    return out["text"]


def _generate(c: httpx.Client, service: str, text: str, extra: dict[str, Any]) -> dict[str, Any]:
    pred = _check(c.post("/v1/skill-runs/generate", headers={"Prefer": "respond-async"}, json={
        "service": service, "prompt_field": "prompt", "text": text, "input": extra,
    }), f"generate {service}")
    pid, t0 = pred["id"], time.monotonic()
    print(f"[generate] {service} prediction={pid} status={pred['status']}")
    while pred["status"] not in ("succeeded", "failed", "canceled"):
        if time.monotonic() - t0 > POLL_TIMEOUT_S:
            sys.exit(f"[FAIL] {service} prediction {pid} 超时")
        time.sleep(POLL_INTERVAL_S)
        pred = _check(c.get(f"/v1/predictions/{pid}"), f"poll {pid}")
    if pred["status"] != "succeeded":
        sys.exit(f"[FAIL] {service} prediction {pid}: {pred['status']} {pred.get('error')}")
    print(f"[generate] {service} succeeded in {time.monotonic() - t0:.0f}s")
    return pred


def _download_first_image(c: httpx.Client, base: str, pred: dict[str, Any], dest: Path) -> Path:
    urls = _image_urls(pred.get("output"))
    if not urls:
        sys.exit(f"[FAIL] prediction {pred['id']} 没有图片产物: {pred.get('output')}")
    r = c.get(urljoin(base, urls[0]))
    if r.status_code != 200 or not r.content:
        sys.exit(f"[FAIL] 下载 {urls[0]}: HTTP {r.status_code}")
    dest.write_bytes(r.content)
    print(f"[saved] {dest} ({len(r.content)} bytes)")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--model", default="nous-qwen3-8-27b-twolven")
    ap.add_argument("--t2i-service", default="nous-qwen21-text-to-image")
    ap.add_argument("--edit-service", default="nous-qwen21-image-edit")
    ap.add_argument("--env-file", type=Path, default=ENV_FILE)
    ap.add_argument("--out", default="./skill_runs_out")
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # trust_env=False:本机 ALL_PROXY=socks5(mihomo)会让 httpx 走代理且缺 socksio。
    with httpx.Client(base_url=args.base, trust_env=False, timeout=600,
                      headers={"Authorization": f"Bearer {_admin_token(args.env_file)}"}) as c:
        t2i_text = _preview(c, args.model, "qwen-image-t2i.SKILL.md",
                            "一只橘猫趴在黄昏的木窗台上,窗外是老城区的屋顶")
        t2i = _generate(c, args.t2i_service, t2i_text, {"aspect_ratio": ASPECT_RATIO})
        src_img = _download_first_image(c, args.base, t2i, out_dir / "t2i.png")

        data_url = "data:image/png;base64," + base64.b64encode(src_img.read_bytes()).decode()
        edit_text = _preview(c, args.model, "qwen-image-edit.SKILL.md", [
            {"type": "text", "text": "把窗外换成下雪的夜晚,猫保持不变"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ])
        edit = _generate(c, args.edit_service, edit_text, {"image": data_url})
        _download_first_image(c, args.base, edit, out_dir / "edit.png")
    print("[OK] skill-runs 端到端通过 —— 请目检", out_dir.resolve())


if __name__ == "__main__":
    main()
