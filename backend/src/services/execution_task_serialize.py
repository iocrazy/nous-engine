"""ExecutionTask → dict 序列化(任务面板/WS/API 共用)—— 下沉到 services 层
打破 services→api 反向依赖(workflow_runner 曾 import api.routes.execution_tasks._task_to_dict)。

纯逻辑:只依赖 ExecutionTask model + result envelope 形状。路由的 reaper
(collect_referenced_image_uuids,用 session)向下 import 这里的 _image_urls/_uuid。
"""
from __future__ import annotations

from src.models.execution_task import ExecutionTask

def _iter_node_outputs(result: object):
    """PR-5:真实 task.result 形态是 `{"outputs": {node_id: envelope, ...}}` —
    workflow_executor.execute() 包了一层 outputs(spec §3.3)。老的 detect_*_meta
    在 result.values() 找,只匹配「flat result.{node_id}.envelope」shape(不存在)。
    本 helper 同时兼容两种 shape:有 outputs 字段时 yield outputs.values();
    否则 yield result.values() 兜底(旧 fake / 测试用)。"""
    if not isinstance(result, dict):
        return
    outputs = result.get("outputs")
    if isinstance(outputs, dict):
        yield from outputs.values()
        return
    yield from result.values()


def _image_urls(result: object, limit: int = 8) -> list[str]:
    """抽出 result 里所有产物签名 URL,供任务卡缩略图 + 历史画廊用
    (spec 2026-06-09 run-history PR-B)。最多 limit 张。

    Task 7:comfy bridge 节点(comfy_bridge.py ComfyUIWorkflowNode)的产物形状不是单个
    `image_url`,而是 `thumbnails`(ffmpeg 首帧缩略列表)/ `video_url`(原片)/
    `items[].url`(全量产物,含视频)。这三者都经 `write_media` 落进跟图片同一个
    image_output_storage 目录,受同一个 orphan reaper 管(main.py
    image_orphan_reap_loop → collect_referenced_image_uuids → 本函数)——不在这里
    被识别出来,24h 后就会被当无引用 orphan 误删,即便任务历史仍在引用(丢产物 bug,
    不只是显示 bug)。"""
    urls: list[str] = []
    seen: set[str] = set()

    def _add(u: object) -> None:
        if isinstance(u, str) and u and u not in seen:
            seen.add(u)
            urls.append(u)

    for v in _iter_node_outputs(result):
        if not isinstance(v, dict):
            continue
        _add(v.get("image_url"))
        for t in (v.get("thumbnails") or []):
            _add(t)
        _add(v.get("video_url"))
        for item in (v.get("items") or []):
            if isinstance(item, dict):
                _add(item.get("url"))
        if len(urls) >= limit:
            break
    return urls[:limit]


def _uuid_from_image_url(url: str) -> str | None:
    """/files/images/{date}/{uuid}.{ext}?token&expires → {uuid}(文件 stem)。"""
    if not isinstance(url, str) or not url:
        return None
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    return stem or None


def _detect_image_meta(result: object) -> dict:
    """Pluck task_type + size from a workflow result by scanning for the
    image_output envelope shape. Stays None for non-image results so the
    UI can skip the badge entirely.
    """
    out: dict = {"task_type": None, "image_width": None, "image_height": None}
    for v in _iter_node_outputs(result):
        if not isinstance(v, dict):
            continue
        media_type = v.get("media_type")
        is_image = (
            (isinstance(media_type, str) and media_type.startswith("image/"))
            or "image_url" in v
        )
        if is_image:
            out["task_type"] = "image"
            w, h = v.get("width"), v.get("height")
            if isinstance(w, int) and isinstance(h, int):
                out["image_width"] = w
                out["image_height"] = h
            return out
    return out


def _detect_vision_meta(result: object) -> dict:
    """PR-1d:vision lane(LLM 节点带图/音输入)— LLMNode 返回 {text, usage, multimodal=True}。
    `multimodal=True` 是 PR-1d 在 LLMNode invoke/stream 加的标记字段;_detect_vision_meta
    据此把任务归 type=vision(spec ServiceType / 前端 紫橙渐变 + Vision 图标)。
    返 completion_tokens 供 callout 显示。"""
    out: dict = {"task_type": None, "vision_completion_tokens": None}
    for v in _iter_node_outputs(result):
        if not isinstance(v, dict):
            continue
        if v.get("multimodal") is True and isinstance(v.get("text"), str):
            out["task_type"] = "vision"
            usage = v.get("usage") or {}
            out["vision_completion_tokens"] = usage.get("completion_tokens")
            return out
    return out


def _detect_llm_meta(result: object) -> dict:
    """PR-1c:LLM workflow result envelope 识别 —— `text` 字符串 + `usage` dict 是
    LLMNode.invoke/stream 的标准返回形状(见 src/services/nodes/llm.py)。
    返 completion_tokens / prompt_tokens 供前端 callout 显示「47 tokens · 23 tok/s」。"""
    out: dict = {
        "task_type": None,
        "llm_prompt_tokens": None,
        "llm_completion_tokens": None,
    }
    for v in _iter_node_outputs(result):
        if not isinstance(v, dict):
            continue
        # LLM 节点返回 {text: str, usage: {...}, duration_ms: int}
        if isinstance(v.get("text"), str) and isinstance(v.get("usage"), dict):
            out["task_type"] = "llm"
            usage = v["usage"]
            out["llm_prompt_tokens"] = usage.get("prompt_tokens")
            out["llm_completion_tokens"] = usage.get("completion_tokens")
            return out
    return out


def _detect_tts_meta(result: object) -> dict:
    """PR-1b:TTS workflow result envelope 识别 —— audio/* media_type 或 audio_url。
    匹配 ImageBackend 落 image_output_storage 后的形状的 TTS 对应版本(audio_url + duration)。
    返 duration_seconds 供前端音频时长展示。"""
    out: dict = {"task_type": None, "audio_duration_seconds": None}
    for v in _iter_node_outputs(result):
        if not isinstance(v, dict):
            continue
        media_type = v.get("media_type")
        is_audio = (
            (isinstance(media_type, str) and media_type.startswith("audio/"))
            or "audio_url" in v
        )
        if is_audio:
            out["task_type"] = "tts"
            dur = v.get("duration_seconds") or (v.get("meta") or {}).get("duration_seconds")
            if isinstance(dur, (int, float)):
                out["audio_duration_seconds"] = float(dur)
            return out
    return out


def _detect_asr_meta(result: object, input_json: object = None) -> dict:
    """Arc 3(spec 2026-07-20-moss-asr §8):ASR 直连调用 task 的 type 派生。

    与 image/tts/… 的 workflow 节点输出不同 —— api_call_tasks.finalize 落的是 **flat 顶层 dict**
    `{text, segments_count, speakers, audio_seconds}`(非 `{outputs:{...}}` 包裹),所以直接看
    顶层 result,不走 _iter_node_outputs。`audio_seconds` 是判据(ASR 独有;workflow 结果顶层
    不会有此键 → 不误判)。返 audio_seconds + segments_count 供前端卡片摘要。

    PR-9 两段式:running 态 result 还没落(None),result 判据会失效 → 卡片没类型。此时退回
    看 `input_json.kind == "asr"`(create 时写入),同样派生 type=asr,audio_seconds 从
    input_json 取(ffmpeg 归一化后已知)。completed 行为不变(result 判据先命中)。
    """
    out: dict = {"task_type": None, "audio_seconds": None, "segments_count": None}
    if isinstance(result, dict) and "audio_seconds" in result:
        out["task_type"] = "asr"
        secs = result.get("audio_seconds")
        if isinstance(secs, (int, float)):
            out["audio_seconds"] = secs
        sc = result.get("segments_count")
        if isinstance(sc, int):
            out["segments_count"] = sc
        return out
    # running 态兜底:result 未落,靠 create 时写入的 input_json.kind 派生。
    if isinstance(input_json, dict) and input_json.get("kind") == "asr":
        out["task_type"] = "asr"
        secs = input_json.get("audio_seconds")
        if isinstance(secs, (int, float)):
            out["audio_seconds"] = secs
    return out


def _task_to_dict(t: ExecutionTask, *, include_input: bool = True) -> dict:
    d = {
        "id": str(t.id),
        "workflow_id": str(t.workflow_id) if t.workflow_id else None,
        "workflow_name": t.workflow_name,
        "status": t.status,
        "nodes_total": t.nodes_total,
        "nodes_done": t.nodes_done,
        "current_node": t.current_node,
        "result": t.result,
        "error": t.error,
        "duration_ms": t.duration_ms,
        # 出图缩略(签名 URL 列表)—— 任务卡缩略图 + 历史画廊(PR-B)。
        "output_thumbnails": _image_urls(t.result),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }
    if include_input:
        d["input_json"] = t.input_json
    # 顺序:image 先(workflow 多含 image_output)→ tts → vision(LLM 带图)→ llm(纯文本)。
    # 关键:vision 必须在 LLM 之前 —— 两者 envelope 都是 {text, usage},vision 多一个
    # multimodal=True 标记。先 vision 命中就归 vision,否则才 LLM。
    img_meta = _detect_image_meta(t.result)
    d.update(img_meta)
    if img_meta.get("task_type") is None:
        # Arc 3:ASR 直连调用 task 是 flat 顶层 result(audio_seconds 判据),
        # 与 workflow 节点输出形状不同 —— 先查 asr,命中就归 asr。
        # PR-9:running 态 result 为空时,退回 input_json.kind 判据(见 _detect_asr_meta)。
        asr_meta = _detect_asr_meta(t.result, t.input_json)
        if asr_meta.get("task_type"):
            d["task_type"] = asr_meta["task_type"]
            d["audio_seconds"] = asr_meta["audio_seconds"]
            d["segments_count"] = asr_meta["segments_count"]
        elif (tts_meta := _detect_tts_meta(t.result)).get("task_type"):
            d["task_type"] = tts_meta["task_type"]
            d["audio_duration_seconds"] = tts_meta["audio_duration_seconds"]
        else:
            vision_meta = _detect_vision_meta(t.result)
            if vision_meta.get("task_type"):
                d["task_type"] = vision_meta["task_type"]
                d["vision_completion_tokens"] = vision_meta["vision_completion_tokens"]
            else:
                llm_meta = _detect_llm_meta(t.result)
                if llm_meta.get("task_type"):
                    d["task_type"] = llm_meta["task_type"]
                    d["llm_prompt_tokens"] = llm_meta["llm_prompt_tokens"]
                    d["llm_completion_tokens"] = llm_meta["llm_completion_tokens"]
    # PR-1a/1b/1c/1d(2026-05-27 任务面板重置 spec §State model):显式 `type` 字段
    # (image / tts / llm / vision),对应前端 ServiceType。type=None → 旧 fake / 未识别
    # workflow,前端 Other 兜底。
    d["type"] = d.get("task_type")
    return d
