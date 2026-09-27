"""Image output node (render-only sink).

2026-09-26 起自建图像引擎(flux2-components 细粒度图、image runner 派发)已物理删除,
出图只走 ComfyUI 桥(`comfy_bridge.py`)。`image_output` 作为终端展示节点保留 ——
上游产出 image_url 的节点(如 ComfyUI 工作流节点)连到它。
"""

from __future__ import annotations

from src.services.nodes.registry import register


@register("image_output")
class ImageOutputNode:
    """Render-only sink. Stable envelope: {image_url, media_type, width, height}.
    image_url is the canonical (and only) render path — the signed URL HMAC'd
    against ADMIN_SESSION_SECRET.
    """

    async def invoke(self, data: dict, inputs: dict) -> dict:
        return {
            "image_url": inputs.get("image_url"),
            "media_type": inputs.get("media_type", "image/png"),
            "width": inputs.get("width"),
            "height": inputs.get("height"),
        }
