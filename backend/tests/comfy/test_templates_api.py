import pytest

WF = {"138": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": ""}},
      "92": {"class_type": "SaveVideo", "inputs": {}}}


@pytest.mark.asyncio
async def test_create_template_creates_service(client):
    r = await client.post("/api/v1/comfy-templates", json={"name": "nous-minimax-h3-r2v", "workflow": WF})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["service_name"] == "nous-minimax-h3-r2v" and body["node_count"] == 2
    svc = await client.get("/v1/services/nous-minimax-h3-r2v/schema")
    assert svc.status_code == 200
    assert svc.json()["source_type"] == "comfy_template"


@pytest.mark.asyncio
async def test_mapping_roundtrip_and_stale_detection(client):
    r = await client.post("/api/v1/comfy-templates", json={"name": "nous-tpl-map", "workflow": WF})
    tid = r.json()["id"]
    mapping = {"exposed_params": [{"key": "prompt", "label": "提示词", "type": "string",
                                   "comfy_node_id": "138", "comfy_input": "value", "required": True}]}
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)).status_code == 200
    detail = (await client.get(f"/api/v1/comfy-templates/{tid}")).json()
    assert detail["exposed_params"][0]["key"] == "prompt"
    # 重新上传丢掉 138 节点 → prompt 映射失效
    r2 = await client.put(f"/api/v1/comfy-templates/{tid}",
                          json={"workflow": {"92": WF["92"]}})
    assert r2.json()["stale_keys"] == ["prompt"]


@pytest.mark.asyncio
async def test_mapping_constraints_surface_in_service_schema(client):
    """I3 fix:min/max/step/options 存进 exposed_inputs 时嵌套在 `constraints` 下
    (comfy_templates.py::_mapping_to_exposed_input),`/v1/services/{name}/schema`
    的 input_schema 必须能从这个嵌套形状里把 enum/minimum/maximum 解出来——旧代码
    的 service_schema.py::_input_property 只认 node.yaml widget(桥节点 class_type
    是 "comfyui_workflow",从来没有 widget 定义),min/max/enum 全部丢失。"""
    r = await client.post("/api/v1/comfy-templates", json={"name": "nous-tpl-constraints", "workflow": WF})
    tid = r.json()["id"]
    mapping = {"exposed_params": [
        {"key": "steps", "label": "步数", "type": "integer",
         "comfy_node_id": "138", "comfy_input": "value",
         "min": 1, "max": 150, "step": 1, "required": True},
        {"key": "mode", "label": "模式", "type": "string",
         "comfy_node_id": "138", "comfy_input": "value",
         "options": ["fast", "quality"], "required": False},
    ]}
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)).status_code == 200

    schema = (await client.get("/v1/services/nous-tpl-constraints/schema")).json()
    props = schema["input_schema"]["properties"]
    assert props["steps"]["minimum"] == 1
    assert props["steps"]["maximum"] == 150
    assert props["mode"]["enum"] == ["fast", "quality"]

    # Editor round-trip (GET template detail) un-nests constraints back to the
    # flat ComfyExposedParam shape the frontend editor works with.
    detail = (await client.get(f"/api/v1/comfy-templates/{tid}")).json()
    by_key = {p["key"]: p for p in detail["exposed_params"]}
    assert by_key["steps"]["min"] == 1
    assert by_key["steps"]["max"] == 150
    assert by_key["mode"]["options"] == ["fast", "quality"]


@pytest.mark.asyncio
async def test_create_template_seeds_exposed_outputs(client):
    """I1 fix:create_template 现在种一个 exposed_outputs 条目(video_url,指向桥快照的
    终端 video_output 节点 id="out"),否则 /schema 的 output_schema 永远是空
    {"properties": {}},Playground 的 SchemaDrivenOutput 拿不到任何 declared output
    可渲染,视频播放器出不来。"""
    r = await client.post("/api/v1/comfy-templates", json={"name": "nous-tpl-outputs", "workflow": WF})
    assert r.status_code == 201, r.text

    schema = (await client.get("/v1/services/nous-tpl-outputs/schema")).json()
    out_props = schema["output_schema"]["properties"]
    assert "video_url" in out_props
    assert out_props["video_url"]["type"] == "string"
    assert out_props["video_url"]["format"] == "uri"


@pytest.mark.asyncio
async def test_delete_removes_service(client):
    r = await client.post("/api/v1/comfy-templates", json={"name": "nous-tpl-del", "workflow": WF})
    tid = r.json()["id"]
    assert (await client.delete(f"/api/v1/comfy-templates/{tid}")).status_code == 204
    assert (await client.get("/v1/services/nous-tpl-del/schema")).status_code == 404


@pytest.mark.asyncio
async def test_file_field_never_gets_enum_whitelist(client):
    """ComfyUI 给文件型输入的 options 是 **sidecar 已有文件清单**(LoadImage.image →
    input/ 目录),不是取值域。存成 constraints.enum 会让后端把"上传一张新图"判成非法
    (2026-08-12 实机:Playground 传 data URI 必 400 must be one of [...])。"""
    wf = {"137": {"class_type": "LoadImage", "inputs": {"image": "old.jpg"}},
          "92": {"class_type": "SaveVideo", "inputs": {}}}
    r = await client.post("/api/v1/comfy-templates", json={"name": "nous-tpl-fileenum", "workflow": wf})
    tid = r.json()["id"]
    await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json={"exposed_params": [
        {"key": "image", "label": "参考图", "type": "image", "required": True,
         "options": ["old.jpg", "example.png"],   # sidecar 现有文件,不该成为白名单
         "comfy_node_id": "137", "comfy_input": "image"},
        {"key": "mode", "label": "模式", "type": "string",
         "options": ["fast", "quality"],          # 真枚举:仍然保留
         "comfy_node_id": "137", "comfy_input": "mode"},
    ]})
    schema = (await client.get("/v1/services/nous-tpl-fileenum/schema")).json()["input_schema"]["properties"]
    assert "enum" not in schema["image"], "文件类字段不该带 enum 白名单"
    assert schema["mode"]["enum"] == ["fast", "quality"], "非文件类的真枚举要保留"
