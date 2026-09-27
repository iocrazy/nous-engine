"""image-io 节点 executor — 图像输入(上传图 → 落盘签 URL → image 端口)。

image_input 是 **inline 节点**(主进程 event loop,CPU:base64 解码 + 写盘 + 量宽高):
把前端上传的 base64 data URI 落盘到 image_output_storage,产出
`{image_url, media_type, width, height}` —— 下游 image 类型节点(调色/合并/对比等)
经 inputs 拿到 image_url(签名 URL),各自解析回本地磁盘读图。

为何落盘签 URL 而非直接透传 base64:大图走进程间 pipe 传 base64 是反模式(可能几 MB)。
落盘后只过一个签名 URL 字符串。
"""
from __future__ import annotations

import base64
import re

# 本站图 storage URL 的 date/uuid/ext 合法字符(防路径遍历:URL 来自前端拖拽=用户可控)。
_SAFE_SEG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _passthrough_existing_image(src: str) -> dict:
    """已生成图(本站签名 URL `/files/images/<date>/<uuid>.<ext>?token=...`)回流当输入(spec 2026-06-12
    出图拖到输入):**不重新落盘**(图已在盘),解析 date/uuid/ext → resolve_path 校验在盘 → 重签延 TTL
    (防生成图签名 1h 过期)。只接本站 storage URL + segment 白名单 + resolve_path 限定 outputs root
    → 防 SSRF / 路径遍历任意读盘。"""
    from urllib.parse import urlparse  # noqa: PLC0415

    from src.config import get_settings  # noqa: PLC0415
    from src.services.image_output_storage import resolve_path, sign_existing_image  # noqa: PLC0415

    parts = urlparse(src).path.strip("/").split("/")  # [files, images, <date>, <uuid>.<ext>]
    if len(parts) < 4 or parts[0] != "files" or parts[1] != "images":
        raise RuntimeError("图像输入:无法解析本站图 URL(应为 /files/images/<date>/<uuid>.<ext>)")
    date = parts[-2]
    uuid_str, _, ext = parts[-1].rpartition(".")
    ext = ext or "png"
    if not (_SAFE_SEG.match(date) and _SAFE_SEG.match(uuid_str) and _SAFE_SEG.match(ext)):
        raise RuntimeError("图像输入:本站图 URL 含非法字符")
    p = resolve_path(date, uuid_str, ext)
    if not p.exists():
        raise RuntimeError("图像输入:引用的图已不在盘(可能过期清理)—— 请重新生成或上传")
    ttl = int(get_settings().IMAGE_URL_TTL_SECONDS)
    signed, expires = sign_existing_image(date, uuid_str, ext, ttl_seconds=ttl)
    url = signed or f"/files/images/{date}/{uuid_str}.{ext}"  # 无签名密钥(dev/test)→ 裸 URL
    width = height = None
    try:
        from PIL import Image  # noqa: PLC0415
        with Image.open(p) as im:
            width, height = im.size
    except Exception:  # noqa: BLE001 — best-effort metadata
        pass
    return {
        "image_url": url, "image_uuid": uuid_str, "image_expires": expires,
        "media_type": f"image/{ext}", "width": width, "height": height,
    }


async def exec_image_input(data: dict, inputs: dict) -> dict:
    """图像输入 → {image_url, media_type, width, height}。data.image 两种来源:
    ① base64 data URI(上传图)→ 解码 + 落盘签 URL;
    ② 本站已生成图 URL(出图拖到输入,spec 2026-06-12)→ 透传 + 重签延 TTL(不重新落盘)。"""
    src = data.get("image") or ""
    if not src or not isinstance(src, str):
        raise RuntimeError("图像输入:未上传图(data.image 应为 base64 data URI 或本站图 URL)")
    # ② 已生成图回流当输入:本站 storage URL → 透传。
    if "/files/images/" in src:
        return _passthrough_existing_image(src)
    if not src.startswith("data:"):
        raise RuntimeError("图像输入:data.image 应为 base64 data URI 'data:image/...;base64,...' 或本站图 URL")

    # "data:image/png;base64,...." → media_type + 原始 bytes。
    header, _, b64 = src.partition(",")
    media_type = "image/png"
    if header.startswith("data:") and ";" in header:
        media_type = header[len("data:"):].split(";", 1)[0] or "image/png"
    raw = base64.b64decode(b64)

    # 落盘签 URL(NAS_OUTPUTS_PATH + HMAC),同 flux2_vae_decode 出图。
    from src.config import get_settings  # noqa: PLC0415
    from src.services.image_output_storage import write_image  # noqa: PLC0415
    ext = media_type.split("/", 1)[1].split("+", 1)[0] or "png"
    ttl = int(get_settings().IMAGE_URL_TTL_SECONDS)  # PR-4:TTL 归服务层配置(不再读节点 widget)
    record = write_image(raw, ext=ext, ttl_seconds=ttl)

    # 量宽高(可选,失败不致命 —— 下游超分不依赖,只为 UI/meta)。
    width = height = None
    try:
        import io  # noqa: PLC0415

        from PIL import Image  # noqa: PLC0415
        with Image.open(io.BytesIO(raw)) as im:
            width, height = im.size
    except Exception:  # noqa: BLE001 — best-effort metadata
        pass

    return {
        "image_url": record["url"],
        "image_uuid": record["uuid"],
        "image_expires": record["expires"],
        "media_type": media_type,
        "width": width,
        "height": height,
    }


async def exec_image_ref_join(data: dict, inputs: dict) -> dict:
    """参考图合并(多参考编辑):两路上游 image_url → 逗号串单 image 输出。可串联扩 3+ 图
    (上游若已是逗号串原样拼接)。下游 KSampler→runner→引擎全链已按逗号拆。
    单路连线放行(透传)—— 工作流搭一半不该崩;两路全空才是连线错误。"""
    a = inputs.get("image_a") or inputs.get("image_url_a")
    b = inputs.get("image_b") or inputs.get("image_url_b")
    parts = [str(p).strip() for p in (a, b) if p and str(p).strip()]
    if not parts:
        raise RuntimeError("参考图合并:两路输入都没有图 —— 请把上游图像节点连到「参考图 A/B」端口")
    return {"image_url": ",".join(parts)}


async def exec_image_compare(data: dict, inputs: dict) -> dict:
    """图像对比 = 显示型 sink(对比纯前端,从两路上游 node_complete 的 image_url 取图渲染滑动对比)。
    executor 是 no-op:让工作流执行不报「未知节点」,本身不产输出。透传两路 image_url 进 meta
    方便调试/留痕(前端不依赖它)。"""
    return {
        "image_a_url": inputs.get("image_url_a") or inputs.get("image_a"),
        "image_b_url": inputs.get("image_url_b") or inputs.get("image_b"),
    }


EXECUTORS = {
    "image_input": exec_image_input,
    "image_ref_join": exec_image_ref_join,
    "image_compare": exec_image_compare,
}
