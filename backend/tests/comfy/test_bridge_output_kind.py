"""桥的通用修复:按实际产物区分图片/视频信封、模板输出类型推断、ComfyUI 执行错误、
文件入参校验。老视频模板的形状必须一字不变。"""
from __future__ import annotations

import base64

import pytest

import src.services.nodes.comfy_bridge as nb
from src.api.routes.comfy_templates import _bridge_exposed_outputs, _infer_output_kind
from src.services.comfy.client import ComfyError
from src.services.comfy.outputs import history_error
from src.services.comfy.upload_inputs import (
    UploadInputError,
    check_plain_filename,
    decode_data_uri,
)
from src.services.execution_task_serialize import _detect_image_meta, _image_urls
from src.services.nodes.registry import get_node_class
from tests.comfy.test_bridge_node import FakeClient

# ---------- 信封 ----------


def _fake(monkeypatch, history: dict) -> FakeClient:
    fc = FakeClient()

    async def _wait(prompt_id, *, timeout_s, interval_s=2.0, **_kw):
        return history
    fc.wait = _wait
    monkeypatch.setattr(nb, "get_client", lambda: fc)

    async def _no_thumb(_p):
        return None
    monkeypatch.setattr(nb, "extract_first_frame", _no_thumb)

    async def _write(data, *, ext, ttl_seconds=86400):
        return {"url": f"/files/x.{ext}", "uuid": "u", "ext": ext}
    monkeypatch.setattr(nb, "write_media", _write)

    async def _load(tid):
        return {"9": {"class_type": "SaveImage", "inputs": {}},
                "19": {"class_type": "VHS_VideoCombine", "inputs": {}},
                "5": {"class_type": "PreviewImage", "inputs": {}}}, []
    monkeypatch.setattr(nb, "load_template", _load)
    return fc


IMG = {"filename": "a.png", "subfolder": "", "type": "output"}
PREVIEW = {"filename": "p.png", "subfolder": "", "type": "temp"}
VID = {"filename": "v.mp4", "subfolder": "", "type": "output"}


@pytest.mark.asyncio
async def test_image_envelope_has_image_url_and_null_video_url(monkeypatch):
    _fake(monkeypatch, {"outputs": {"9": {"images": [IMG]}, "5": {"images": [PREVIEW]}}})
    out = await get_node_class("comfyui_workflow")().invoke({"template_id": 1}, {})
    assert out["image_url"] == "/files/x.png"
    assert out["video_url"] is None
    assert [i["node_id"] for i in out["items"]] == ["9"]  # 预览图被滤掉


@pytest.mark.asyncio
async def test_video_envelope_unchanged_and_has_no_image_url(monkeypatch):
    """老视频模板:形状仍是 {items, video_url, thumbnails, seed},不多 image_url。"""
    _fake(monkeypatch, {"outputs": {"19": {"gifs": [VID]}, "9": {"images": [IMG]}}})
    out = await get_node_class("comfyui_workflow")().invoke({"template_id": 1}, {})
    assert set(out) == {"items", "video_url", "thumbnails", "seed"}
    assert out["video_url"] == "/files/x.mp4"


@pytest.mark.asyncio
async def test_video_output_node_unwraps_executor_spread_of_image_envelope():
    """执行器对图片信封的 spread 会塞 inputs["outputs"] = 图片 URL 字符串;终端节点
    必须返回信封本身,不能返回那个字符串。"""
    env = {"items": [], "video_url": None, "image_url": "/files/x.png", "thumbnails": [],
           "seed": None}
    node = get_node_class("video_output")()
    assert await node.invoke({}, {**env, "outputs": "/files/x.png"}) == env
    assert await node.invoke({}, {"outputs": env}) == env


def test_task_serializer_types_image_but_not_video_results():
    img = {"outputs": {"bridge": {"items": [{"url": "/f/a.png"}], "video_url": None,
                                  "image_url": "/f/a.png", "thumbnails": [], "seed": None}}}
    vid = {"outputs": {"bridge": {"items": [{"url": "/f/v.mp4"}], "video_url": "/f/v.mp4",
                                  "thumbnails": [], "seed": None}}}
    assert _detect_image_meta(img)["task_type"] == "image"
    assert _detect_image_meta(vid)["task_type"] is None
    assert _image_urls(img) == ["/f/a.png"]


# ---------- ComfyUI 执行错误 ----------


@pytest.mark.asyncio
async def test_execution_error_raises_even_with_partial_outputs(monkeypatch):
    _fake(monkeypatch, {
        "outputs": {"9": {"images": [IMG]}},
        "status": {"status_str": "error", "messages": [
            ["execution_error", {"node_id": "66:54", "node_type": "KSampler",
                                 "exception_type": "RuntimeError",
                                 "exception_message": "boom"}]]}})
    with pytest.raises(ComfyError, match=r"66:54.*KSampler.*boom"):
        await get_node_class("comfyui_workflow")().invoke({"template_id": 1}, {})


def test_history_error_shapes():
    assert history_error({"outputs": {}}) is None
    assert history_error({"status": {"status_str": "success"}}) is None
    assert history_error({"status": {"status_str": "error", "messages": [
        ["execution_interrupted", {}]]}}) == "ComfyUI 渲染被中断"
    assert "未给出错误详情" in history_error({"status": {"status_str": "error"}})
    long = history_error({"status": {"status_str": "error", "messages": [
        ["execution_error", {"exception_message": "x" * 5000}]]}})
    assert len(long) <= 500


# ---------- 模板输出类型 ----------


@pytest.mark.parametrize(("wf", "kind"), [
    ({"9": {"class_type": "SaveImage"}}, "image"),
    ({"19": {"class_type": "VHS_VideoCombine"}, "4": {"class_type": "SaveImage"}}, "video"),
    ({"92": {"class_type": "SaveVideo"}}, "video"),
    ({"1": {"class_type": "PreviewImage"}}, "video"),  # 不认识 → 老默认
    ({}, "video"),
])
def test_infer_output_kind(wf, kind):
    assert _infer_output_kind(wf) == kind


def test_bridge_exposed_outputs_video_is_the_legacy_shape():
    assert _bridge_exposed_outputs("video") == [{
        "key": "video_url", "node_id": "out", "input_name": "video_url",
        "type": "video", "label": "视频"}]
    assert _bridge_exposed_outputs("image")[0]["key"] == "image_url"


@pytest.mark.asyncio
async def test_create_template_output_kind_override(client):
    wf = {"9": {"class_type": "SaveImage", "inputs": {}}}
    r = await client.post("/api/v1/comfy-templates", json={"name": "nous-kind-auto", "workflow": wf})
    assert r.status_code == 201 and r.json()["output_kind"] == "image", r.text
    r = await client.post("/api/v1/comfy-templates",
                          json={"name": "nous-kind-forced", "workflow": wf, "output_kind": "video"})
    assert r.status_code == 201 and r.json()["output_kind"] == "video", r.text
    schema = (await client.get("/v1/services/nous-kind-forced/schema")).json()
    assert list(schema["output_schema"]["properties"]) == ["video_url"]
    r = await client.post("/api/v1/comfy-templates",
                          json={"name": "nous-kind-bad", "workflow": wf, "output_kind": "gif"})
    assert r.status_code in (400, 422), r.text


# ---------- 文件入参 ----------


PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode()


def test_decode_data_uri_ok_and_ext_map():
    raw, ext, mime = decode_data_uri("image", "image", PNG)
    assert (raw, ext, mime) == (b"\x89PNG", "png", "image/png")
    mov = "data:video/quicktime;base64," + base64.b64encode(b"x").decode()
    assert decode_data_uri("v", "video", mov)[1] == "mov"
    # media/file 型不限大类(老模板语义)
    assert decode_data_uri("m", "media", mov)[1] == "mov"


@pytest.mark.parametrize(("ptype", "value", "msg"), [
    ("image", "data:video/mp4;base64,eA==", "image/"),
    ("video", PNG, "video/"),
    ("image", "data:image/png,rawtext", "base64 编码"),
    ("image", "data:image/png;base64,***", "base64 内容"),
    ("image", "data:image/png;base64,", "为空"),
    ("image", "data:;base64,eA==", "mime"),
    ("media", "data:application/x-evil$(id);base64,eA==", "不支持"),
])
def test_decode_data_uri_rejects(ptype, value, msg):
    with pytest.raises(UploadInputError, match=msg.replace("(", r"\(")):
        decode_data_uri("k", ptype, value)


@pytest.mark.parametrize("value", [
    "http://example.com/a.png", "https://x/y", "file:///etc/passwd", "../a.png", "a/b.png",
    "a\\b.png", "a.png [output]", "", " a.png",
])
def test_plain_filename_rejects_urls_and_paths(value):
    with pytest.raises(UploadInputError):
        check_plain_filename("k", value)


def test_plain_filename_allows_bare_names():
    assert check_plain_filename("k", "vosr2_test_input.mp4") == "vosr2_test_input.mp4"
