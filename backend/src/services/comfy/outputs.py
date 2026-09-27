"""ComfyUI history 产物分拣(仿 IC:扩展名定 kind,冗余 preview 图过滤)。"""
from __future__ import annotations

from dataclasses import dataclass

# I2 fix:single source of truth for ext → kind. `image_files.py` (serving
# whitelist) and `image_output_storage.py` (reap allowed_ext) both import
# `SERVABLE_EXTS` derived below instead of keeping their own hand-rolled
# lists — those had drifted out of sync (route whitelist was missing
# gif/webm/mov/mkv/mp3/flac/ogg even though the reaper already handled them).
KIND_BY_EXT = {
    "png": "image", "jpg": "image", "jpeg": "image", "webp": "image", "gif": "image",
    "mp4": "video", "webm": "video", "mov": "video", "mkv": "video",
    "wav": "audio", "mp3": "audio", "flac": "audio", "ogg": "audio",
    "txt": "text", "json": "text", "srt": "text",
}
# Extensions servable via the signed-URL /files/images route + reapable by the
# orphan reaper — every image/video/audio kind (not "text": txt/json/srt are
# never written there today and aren't meant to be served as static binaries).
SERVABLE_EXTS: frozenset[str] = frozenset(
    ext for ext, kind in KIND_BY_EXT.items() if kind != "text")
_PREVIEW_HINTS = ("previewimage", "comparer", "imagecompare")


@dataclass
class OutputItem:
    node_id: str
    class_type: str
    filename: str
    subfolder: str
    file_type: str
    kind: str


def classify_ext(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return KIND_BY_EXT.get(ext, "file")


def is_preview_class(class_type: str) -> bool:
    """预览/对比类节点(不是模板的产出端)。`collect_outputs` 与 `graph_prune` 共用。"""
    ct = class_type.lower()
    return any(h in ct for h in _PREVIEW_HINTS)


def collect_outputs(history: dict, graph: dict) -> list[OutputItem]:
    cands: list[OutputItem] = []
    for node_id, node_out in (history.get("outputs") or {}).items():
        ct = str((graph.get(str(node_id)) or {}).get("class_type") or "")
        for key in ("images", "videos", "audio", "gifs", "files"):
            for item in node_out.get(key) or []:
                if not isinstance(item, dict) or "filename" not in item:
                    continue
                cands.append(OutputItem(
                    node_id=str(node_id), class_type=ct,
                    filename=str(item["filename"]),
                    subfolder=str(item.get("subfolder", "")),
                    file_type=str(item.get("type", "output")),
                    kind=classify_ext(str(item["filename"]))))
    has_primary_image = any(c.kind == "image" and not is_preview_class(c.class_type) for c in cands)
    return [c for c in cands
            if not (c.kind == "image" and has_primary_image and is_preview_class(c.class_type))]


# `execution_error` 的异常消息可能很长,截断后再进 task.error / prediction.error。
_ERROR_MSG_LIMIT = 500


def history_error(history: dict) -> str | None:
    """ComfyUI `/history/{id}` 条目 → 失败说明;没失败返回 None。

    `ComfyClient.wait()` 只等「history 里出现这条记录」,**不区分成功失败** —— 节点抛异常
    (OOM、模型缺失、坏视频…)时记录照样出现,`status.status_str == "error"`,`outputs`
    可能空、也可能是出错前已写好的半截产物。不看这里的话,前者只剩一句含糊的「未产出任何
    产物」,后者更糟:半截产物被当成功返回。
    """
    status = history.get("status") if isinstance(history, dict) else None
    if not isinstance(status, dict) or status.get("status_str") != "error":
        return None
    for msg in status.get("messages") or []:
        if not isinstance(msg, (list, tuple)) or len(msg) != 2:
            continue
        event, detail = msg
        detail = detail if isinstance(detail, dict) else {}
        if event == "execution_error":
            where = f"节点 {detail.get('node_id')}({detail.get('node_type')})"
            what = f"{detail.get('exception_type') or ''}: {detail.get('exception_message') or ''}"
            return f"ComfyUI 执行失败:{where} {what.strip(': ')}"[:_ERROR_MSG_LIMIT]
        if event == "execution_interrupted":
            return "ComfyUI 渲染被中断"
    return "ComfyUI 执行失败(sidecar 未给出错误详情)"
