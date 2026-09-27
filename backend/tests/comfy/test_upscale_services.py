"""三个 ComfyUI 放大桥服务(SeedVR2 图片 / VOSR2 图片 / VOSR2 视频)端到端。

模板与 mapping 直接读仓库里提交的部署产物 `docs/replications/upscale/`(官方前端
`app.graphToPrompt()` 转出的 API prompt + exposed_params)—— 测的就是要上线的那份数据,
不是手抄的替身。ComfyUI sidecar 用 FakeClient 替身,绝不碰真 ComfyUI。
"""
from __future__ import annotations

import asyncio
import base64
import json
import secrets as _secrets
from pathlib import Path

import bcrypt
import pytest
from sqlalchemy import select

import src.services.nodes.comfy_bridge as nb
from src.models.api_gateway import ApiKeyGrant
from src.models.database import get_session_factory
from src.models.instance_api_key import InstanceApiKey
from src.models.service_instance import ServiceInstance
from src.services.comfy.client import ComfyError

ARTIFACTS = Path(__file__).resolve().parents[3] / "docs" / "replications" / "upscale"

PNG1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==")
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG1PX).decode()
MP4_URI = "data:video/mp4;base64," + base64.b64encode(b"\x00\x00\x00\x18ftypmp42").decode()

# service name → (artifact 名, 产物类型, 输出节点, history 产物)
SERVICES = {
    "nous-seedvr2-image-upscale": (
        "seedvr2-image-upscale", "image", "9",
        {"images": [{"filename": "SeedVR2_00001_.png", "subfolder": "", "type": "output"}]}),
    "nous-vosr2-image-upscale": (
        "vosr2-image-upscale", "image", "4",
        {"images": [{"filename": "nous-vosr2-image_00001_.png", "subfolder": "", "type": "output"}]}),
    "nous-vosr2-video-upscale": (
        "vosr2-video-upscale", "video", "19",
        {"gifs": [{"filename": "nous-vosr2-video_00001.mp4", "subfolder": "", "type": "output",
                   "format": "video/h264-mp4", "frame_rate": 24.0}]}),
}
IMAGE_SERVICES = [n for n, v in SERVICES.items() if v[1] == "image"]
MEDIA_KEY = {"image": "image", "video": "video"}
MEDIA_URI = {"image": PNG_URI, "video": MP4_URI}

# 调用方绝不能改到的模型/权重输入(class_type → input 名)。
MODEL_INPUTS = {
    "UNETLoader": {"unet_name", "weight_dtype"},
    "VAELoader": {"vae_name"},
    "VOSR2ModelLoader": {"model"},
}


def _artifact(name: str) -> tuple[dict, dict]:
    wf = json.loads((ARTIFACTS / f"{name}.api.json").read_text())
    mapping = json.loads((ARTIFACTS / f"{name}.mapping.json").read_text())
    return wf, mapping


class UpscaleFakeClient:
    """sidecar 替身:记录上传/提交,wait 返回可配置的 history。"""

    def __init__(self, history: dict | None = None, submit_error: ComfyError | None = None):
        self.uploaded: list[tuple[str, str]] = []
        self.submitted: dict | None = None
        self.history = history
        self.submit_error = submit_error

    async def upload_image(self, filename, content, mime="image/png"):
        self.uploaded.append((filename, mime))
        return f"up_{filename}"

    async def submit(self, graph):
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted = graph
        return "p1"

    async def wait(self, prompt_id, *, timeout_s, interval_s=2.0, **_kw):
        return self.history

    async def download(self, item):
        return b"PNGDATA" if item["filename"].endswith(".png") else b"MP4DATA"

    async def interrupt(self) -> None:
        return None


def _ok_history(service: str) -> dict:
    _, _, node, out = SERVICES[service]
    return {"outputs": {node: out},
            "status": {"status_str": "success", "completed": True, "messages": []}}


@pytest.fixture
def no_thumbnail(monkeypatch):
    async def _none(_path):
        return None
    monkeypatch.setattr(nb, "extract_first_frame", _none)


def _use(monkeypatch, fc: UpscaleFakeClient) -> UpscaleFakeClient:
    monkeypatch.setattr(nb, "get_client", lambda: fc)
    return fc


async def _deploy(client, service: str) -> str:
    """按报告里的部署步骤建服务:POST 模板(不传 output_kind,走推断)→ PUT mapping。"""
    artifact = SERVICES[service][0]
    wf, mapping = _artifact(artifact)
    r = await client.post("/api/v1/comfy-templates", json={"name": service, "workflow": wf})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r2 = await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)
    assert r2.status_code == 200, r2.text
    return tid


async def _mint_key(service: str | None) -> str:
    """铸一把 M:N key;service 给了就授权它,None = 无任何 grant。"""
    raw = f"sk-up-{_secrets.token_hex(8)}"
    async with get_session_factory()() as s:
        key = InstanceApiKey(
            instance_id=None, label="t",
            key_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
            key_prefix=raw[:10], is_active=True)
        s.add(key)
        await s.commit()
        await s.refresh(key)
        if service is not None:
            svc = (await s.execute(
                select(ServiceInstance).where(ServiceInstance.name == service))).scalar_one()
            s.add(ApiKeyGrant(api_key_id=key.id, service_id=svc.id, status="active"))
            await s.commit()
    return raw


async def _predict(client, service: str, inputs: dict, headers: dict | None = None):
    return await client.post(
        f"/v1/services/{service}/predictions", json={"input": inputs}, headers=headers or {})


def _media_input(service: str) -> dict:
    kind_in = "video" if service.endswith("video-upscale") else "image"
    return {MEDIA_KEY[kind_in]: MEDIA_URI[kind_in]}


# ---------- 1 / 9:部署产物本身 ----------


@pytest.mark.parametrize("service", list(SERVICES))
def test_artifact_mapping_targets_real_nodes_and_never_models(service):
    """mapping 的每个 (comfy_node_id, comfy_input) 都在官方前端转出的 API prompt 里;
    模型加载器的权重输入一个都不暴露(9:模型路径/文件名不可由外部覆盖)。"""
    wf, mapping = _artifact(SERVICES[service][0])
    for p in mapping["exposed_params"]:
        node = wf.get(p["comfy_node_id"])
        assert node is not None, p
        assert p["comfy_input"] in node["inputs"], p
        assert p["comfy_input"] not in MODEL_INPUTS.get(node["class_type"], set()), p
    # 权重在快照里固定死
    classes = {n["class_type"]: n["inputs"] for n in wf.values()}
    if "UNETLoader" in classes:
        assert classes["UNETLoader"]["unet_name"] == "seedvr2_7b_int8_convrot.safetensors"
        assert classes["VAELoader"]["vae_name"] == "seedvr2_ema_vae_fp16.safetensors"
    if "VOSR2ModelLoader" in classes:
        assert classes["VOSR2ModelLoader"]["model"] == "VOSR2"


def test_vosr2_snapshots_only_contain_their_branch():
    img, _ = _artifact("vosr2-image-upscale")
    vid, _ = _artifact("vosr2-video-upscale")
    assert {n["class_type"] for n in img.values()} == {
        "LoadImage", "VOSR2ModelLoader", "VOSR2Upscale", "SaveImage"}
    assert {n["class_type"] for n in vid.values()} == {
        "VHS_LoadVideo", "ImageScaleToMaxDimension", "VOSR2ModelLoader", "VOSR2Upscale",
        "VHS_VideoInfo", "VHS_VideoCombine"}
    # 帧率默认保留原视频:VideoCombine.frame_rate 连到 VideoInfo 的 loaded_fps(槽 5)
    assert vid["19"]["inputs"]["frame_rate"] == ["12", 5]


@pytest.mark.asyncio
async def test_services_names_and_template_types(client):
    for service, (_, kind, _, _) in SERVICES.items():
        await _deploy(client, service)
        r = await client.get(f"/v1/services/{service}/schema")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["service"] == service
        assert body["source_type"] == "comfy_template"
        out_props = body["output_schema"]["properties"]
        want = "image_url" if kind == "image" else "video_url"
        assert list(out_props) == [want]
        assert out_props[want] == {"type": "string", "format": "uri",
                                   "description": "图片" if kind == "image" else "视频"}
        media_key = "video" if kind == "video" else "image"
        assert body["input_schema"]["required"] == [media_key]


def test_service_names_carry_nous_prefix():
    from src.services.workflow_snapshot import NAME_RE
    for service in SERVICES:
        assert NAME_RE.match(service), service


# ---------- 2 / 3 / 4:同步调用、产物 schema、data URI 上传 ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("service", IMAGE_SERVICES)
async def test_image_service_returns_image_schema(client, monkeypatch, no_thumbnail, service):
    fc = _use(monkeypatch, UpscaleFakeClient(_ok_history(service)))
    await _deploy(client, service)
    key = await _mint_key(service)

    r = await _predict(client, service, {"image": PNG_URI},
                       headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    pred = r.json()
    assert pred["status"] == "succeeded", pred
    out = pred["output"]["outputs"]["out"]
    assert out["image_url"].split("?")[0].endswith(".png")
    assert out["video_url"] is None  # 不伪造视频地址
    assert [i["kind"] for i in out["items"]] == ["image"]
    assert out["items"][0]["url"] == out["image_url"]

    # data URI → 上传到 sidecar,图里写的是 sidecar 回的文件名
    assert len(fc.uploaded) == 1 and fc.uploaded[0][0].endswith(".png")
    load_node = {"nous-seedvr2-image-upscale": "1", "nous-vosr2-image-upscale": "3"}[service]
    assert fc.submitted[load_node]["inputs"]["image"] == f"up_{fc.uploaded[0][0]}"


@pytest.mark.asyncio
async def test_video_service_returns_video_schema(client, monkeypatch, no_thumbnail):
    service = "nous-vosr2-video-upscale"
    fc = _use(monkeypatch, UpscaleFakeClient(_ok_history(service)))
    await _deploy(client, service)
    key = await _mint_key(service)

    r = await _predict(client, service, {"video": MP4_URI},
                       headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    pred = r.json()
    assert pred["status"] == "succeeded", pred
    out = pred["output"]["outputs"]["out"]
    assert out["video_url"].split("?")[0].endswith(".mp4")
    assert "image_url" not in out  # 视频结果不带 image_url(否则任务面板误判成图片)
    assert [i["kind"] for i in out["items"]] == ["video"]
    assert out["thumbnails"] == []  # ffmpeg 已 stub

    assert fc.uploaded == [(fc.uploaded[0][0], "video/mp4")]
    assert fc.uploaded[0][0].endswith(".mp4")
    assert fc.submitted["18"]["inputs"]["video"] == f"up_{fc.uploaded[0][0]}"


@pytest.mark.asyncio
async def test_vosr2_video_defaults_are_applied(client, monkeypatch, no_thumbnail):
    """只传 video:其余参数落 mapping default(frame_load_cap=150 限帧防主机 RAM OOM,
    force_rate=0 原帧率)。tile_size=512 / vae_tile_size=1024 是用户 2026-09-27 定的默认:
    不分块时 640×360 4s@24fps ×4 在 VOSR2Upscale 要 225GiB 直接 OOM,开分块实测 214s 跑通。"""
    service = "nous-vosr2-video-upscale"
    fc = _use(monkeypatch, UpscaleFakeClient(_ok_history(service)))
    await _deploy(client, service)
    r = await _predict(client, service, {"video": MP4_URI})
    assert r.json()["status"] == "succeeded", r.text
    g = fc.submitted
    want = {"upscale": 4, "seed": 42, "color_alignment": "wavelet", "tile_size": 512,
            "tile_overlap": 32, "vae_tile_size": 1024, "vae_tile_overlap": 32}
    assert {k: g["8"]["inputs"][k] for k in want} == want
    assert g["7"]["inputs"] == {"model": "VOSR2", "dtype": "default"}
    assert g["22"]["inputs"]["upscale_method"] == "bicubic"
    assert g["22"]["inputs"]["largest_size"] == 640
    assert g["18"]["inputs"]["force_rate"] == 0
    assert g["18"]["inputs"]["frame_load_cap"] == 150
    assert g["18"]["inputs"]["select_every_nth"] == 1
    assert g["19"]["inputs"]["format"] == "video/h264-mp4"
    assert g["19"]["inputs"]["frame_rate"] == ["12", 5]  # 仍连着原视频帧率


@pytest.mark.asyncio
async def test_seedvr2_params_land_on_expanded_subgraph_nodes(client, monkeypatch, no_thumbnail):
    service = "nous-seedvr2-image-upscale"
    fc = _use(monkeypatch, UpscaleFakeClient(_ok_history(service)))
    await _deploy(client, service)
    r = await _predict(client, service, {
        "image": PNG_URI, "scale_multiplier": 3.5, "seed": 7, "sampler_name": "heun",
        "scheduler": "karras", "denoise": 0.6, "color_correction_method": "lab"})
    assert r.json()["status"] == "succeeded", r.text
    g = fc.submitted
    assert g["66:57"]["inputs"]["resize_type.multiplier"] == 3.5
    assert {k: g["66:54"]["inputs"][k] for k in ("seed", "sampler_name", "scheduler", "denoise")} \
        == {"seed": 7, "sampler_name": "heun", "scheduler": "karras", "denoise": 0.6}
    assert g["66:59"]["inputs"]["color_correction_method"] == "lab"


# ---------- 9:模型不可覆盖 ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(SERVICES))
async def test_model_inputs_cannot_be_overridden(client, monkeypatch, no_thumbnail, service):
    fc = _use(monkeypatch, UpscaleFakeClient(_ok_history(service)))
    await _deploy(client, service)
    evil = {"unet_name": "../../etc/passwd", "vae_name": "x.safetensors", "model": "/tmp/evil",
            "weight_dtype": "fp8_e4m3fn", "template_id": 999}
    r = await _predict(client, service, {**_media_input(service), **evil})
    assert r.json()["status"] == "succeeded", r.text
    for node in fc.submitted.values():
        for inp in MODEL_INPUTS.get(node["class_type"], ()):
            wf, _ = _artifact(SERVICES[service][0])
            fixed = next(n for n in wf.values() if n["class_type"] == node["class_type"])
            assert node["inputs"][inp] == fixed["inputs"][inp]


# ---------- 输入校验 ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(("service", "bad"), [
    ("nous-seedvr2-image-upscale", {"sampler_name": "not-a-sampler"}),
    ("nous-seedvr2-image-upscale", {"denoise": 1.5}),
    ("nous-vosr2-image-upscale", {"color_alignment": "lab"}),
    ("nous-vosr2-image-upscale", {"dtype": "fp8"}),
    ("nous-vosr2-image-upscale", {"upscale": 0}),
    ("nous-vosr2-video-upscale", {"output_format": "image/gif"}),
    ("nous-vosr2-video-upscale", {"select_every_nth": 0}),
    # 0 在 VHS 里是「全部帧」—— 等于绕过上限,必须拒
    ("nous-vosr2-video-upscale", {"frame_load_cap": 0}),
    ("nous-vosr2-video-upscale", {"frame_load_cap": 601}),
    ("nous-vosr2-video-upscale", {"skip_first_frames": 10001}),
])
async def test_invalid_inputs_rejected_before_render(client, monkeypatch, service, bad):
    fc = _use(monkeypatch, UpscaleFakeClient(_ok_history(service)))
    await _deploy(client, service)
    r = await _predict(client, service, {**_media_input(service), **bad})
    assert r.status_code == 422, r.text
    assert fc.submitted is None


@pytest.mark.asyncio
async def test_missing_required_media_rejected(client, monkeypatch):
    fc = _use(monkeypatch, UpscaleFakeClient(_ok_history("nous-vosr2-video-upscale")))
    await _deploy(client, "nous-vosr2-video-upscale")
    r = await _predict(client, "nous-vosr2-video-upscale", {"upscale": 2})
    assert r.status_code == 422, r.text
    assert fc.submitted is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("service", "value", "msg"), [
    ("nous-vosr2-video-upscale", PNG_URI, "video/"),                  # 图片塞进视频字段
    ("nous-vosr2-image-upscale", MP4_URI, "image/"),                  # 视频塞进图片字段
    ("nous-vosr2-image-upscale", "http://169.254.169.254/x.png", "URL"),  # 不抓远程
    ("nous-seedvr2-image-upscale", "../../secret.png", "URL"),        # 不给路径
    ("nous-seedvr2-image-upscale", "x.png [output]", "URL"),          # 不给注解
    ("nous-vosr2-image-upscale", "data:image/png;base64,@@@", "base64"),
    ("nous-vosr2-video-upscale", "data:video/x-msvideo;base64,eA==", "不支持的视频格式"),
])
async def test_bad_media_fails_without_rendering(client, monkeypatch, service, value, msg):
    fc = _use(monkeypatch, UpscaleFakeClient(_ok_history(service)))
    await _deploy(client, service)
    field = "video" if service.endswith("video-upscale") else "image"
    r = await _predict(client, service, {field: value})
    pred = r.json()
    assert pred["status"] == "failed", pred
    assert msg in pred["error"], pred
    assert fc.submitted is None and fc.uploaded == []


# ---------- 5:API key grant ----------


@pytest.mark.asyncio
async def test_api_key_grant_required(client, monkeypatch, no_thumbnail):
    service = "nous-vosr2-image-upscale"
    _use(monkeypatch, UpscaleFakeClient(_ok_history(service)))
    await _deploy(client, service)
    await _deploy(client, "nous-seedvr2-image-upscale")

    granted = await _mint_key(service)
    r = await _predict(client, service, {"image": PNG_URI},
                       headers={"Authorization": f"Bearer {granted}"})
    assert r.status_code == 200 and r.json()["status"] == "succeeded", r.text

    # 授权给别的服务的 key、没有任何 grant 的 key —— 一律拒。(不是 InstanceApiKey 的
    # bearer 会退回 admin-session 校验,测试里 admin 门是关的,故不在此列。)
    other = await _mint_key("nous-seedvr2-image-upscale")
    none = await _mint_key(None)
    for bad in (other, none):
        r = await _predict(client, service, {"image": PNG_URI},
                           headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code in (401, 403, 404), (bad, r.status_code, r.text)


# ---------- 6:异步 + 轮询 ----------


async def _poll(client, pid, headers, timeout: float = 5.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    body: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/predictions/{pid}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("succeeded", "failed", "canceled"):
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"prediction {pid} 未到终态:{body}")


@pytest.mark.asyncio
@pytest.mark.parametrize("service", list(SERVICES))
async def test_async_prediction_and_polling(client, monkeypatch, no_thumbnail, service):
    _use(monkeypatch, UpscaleFakeClient(_ok_history(service)))
    await _deploy(client, service)
    key = await _mint_key(service)
    headers = {"Authorization": f"Bearer {key}"}

    r = await _predict(client, service, _media_input(service),
                       headers={**headers, "Prefer": "respond-async"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] in ("starting", "processing")
    final = await _poll(client, r.json()["id"], headers)
    assert final["status"] == "succeeded", final
    out = final["output"]["outputs"]["out"]
    want = "video_url" if SERVICES[service][1] == "video" else "image_url"
    assert out[want]


# ---------- 7:ComfyUI 错误不包装成成功 ----------


EXEC_ERROR_HISTORY = {
    # 真 ComfyUI:节点抛异常时 history 照样出现,可能还带着出错前写好的半截产物。
    "outputs": {"4": {"images": [{"filename": "partial.png", "subfolder": "", "type": "output"}]}},
    "status": {"status_str": "error", "completed": False, "messages": [
        ["execution_start", {"prompt_id": "p1"}],
        ["execution_error", {"prompt_id": "p1", "node_id": "1", "node_type": "VOSR2Upscale",
                             "exception_type": "torch.OutOfMemoryError",
                             "exception_message": "CUDA out of memory"}],
    ]},
}


@pytest.mark.asyncio
async def test_comfy_execution_error_propagates(client, monkeypatch, no_thumbnail):
    service = "nous-vosr2-image-upscale"
    _use(monkeypatch, UpscaleFakeClient(EXEC_ERROR_HISTORY))
    await _deploy(client, service)
    for prefer in (None, "respond-async"):
        r = await _predict(client, service, {"image": PNG_URI},
                           headers={"Prefer": prefer} if prefer else {})
        pred = r.json() if prefer is None else await _poll(client, r.json()["id"], {})
        assert pred["status"] == "failed", pred
        assert pred["output"] is None
        assert "CUDA out of memory" in pred["error"] and "VOSR2Upscale" in pred["error"]


@pytest.mark.asyncio
async def test_comfy_prompt_rejection_propagates(client, monkeypatch):
    service = "nous-seedvr2-image-upscale"
    _use(monkeypatch, UpscaleFakeClient(
        submit_error=ComfyError("ComfyUI 拒绝了工作流:KSampler.sampler_name 不合法", status_code=422)))
    await _deploy(client, service)
    pred = (await _predict(client, service, {"image": PNG_URI})).json()
    assert pred["status"] == "failed", pred
    assert "sampler_name" in pred["error"]


# ---------- 旧图片模板的输出契约修复(fix-image-outputs.sh 发的就是这个 PATCH) ----------


FIX_BODY = {"exposed_outputs": [{"key": "image_url", "node_id": "out", "input_name": "image_url",
                                 "type": "image", "label": "图片"}]}


@pytest.mark.asyncio
async def test_fix_image_outputs_patch_is_accepted_and_idempotent(client):
    """修复前建的图片模板 = SaveImage 工作流 + video_url 契约(用 output_kind=video 复现)。"""
    wf = {"9": {"class_type": "SaveImage", "inputs": {}}}
    r = await client.post("/api/v1/comfy-templates",
                          json={"name": "nous-legacy-image", "workflow": wf, "output_kind": "video"})
    assert r.status_code == 201, r.text
    svc_list = (await client.get("/api/v1/services")).json()
    sid = next(s["id"] for s in svc_list if s["name"] == "nous-legacy-image")
    before = (await client.get(f"/api/v1/services/{sid}")).json()
    assert [o["key"] for o in before["exposed_outputs"]] == ["video_url"]

    for _ in range(2):  # 幂等:第二次同样成功,结果不变
        r = await client.patch(f"/api/v1/services/{sid}", json=FIX_BODY)
        assert r.status_code == 200, r.text
        after = (await client.get(f"/api/v1/services/{sid}")).json()
        sig = [{"key": o["key"], "node_id": str(o["node_id"]), "input_name": o["input_name"]}
               for o in after["exposed_outputs"]]
        assert sig == [{"key": "image_url", "node_id": "out", "input_name": "image_url"}]
        # 其余字段不动
        assert after["exposed_inputs"] == before["exposed_inputs"]
        assert after["workflow_snapshot"] == before["workflow_snapshot"]
        assert after["name"] == before["name"] and after["status"] == before["status"]
    schema = (await client.get("/v1/services/nous-legacy-image/schema")).json()
    assert list(schema["output_schema"]["properties"]) == ["image_url"]
