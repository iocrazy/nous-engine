"""带元数据的 combo 选项(缩略图 + 显示名)——桥映射 → service schema 全链路。

动机:Krea2 的 `easy stylesSelector` 有 275 个 fooocus 风格 / 48 个风格包,纯文本
下拉基本没法用。ComfyUI-Easy-Use 的 `/easyuse/prompt/styles?name=<包>` 现成返回
`{name, name_cn, thumbnail}`,数据齐备,缺的只是把它带到 nous 的 service schema。

设计约束:`enum` **必须保持纯值列表** —— JSON Schema 校验和前端既有逻辑都依赖它。
元数据走同级 `x-option-meta`(JSON Schema 允许扩展关键字),不污染 enum。
裸标量 options 的老行为一字不改。
"""
import pytest

WF = {"138": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": ""}},
      "92": {"class_type": "SaveImage", "inputs": {}}}


async def _mk(client, name):
    r = await client.post("/api/v1/comfy-templates", json={"name": name, "workflow": WF})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_plain_options_unchanged(client):
    """回归:裸标量 options 照旧 → enum 纯值,不产生 x-option-meta。"""
    tid = await _mk(client, "nous-opt-plain")
    mapping = {"exposed_params": [{
        "key": "size", "label": "尺寸", "type": "string",
        "comfy_node_id": "138", "comfy_input": "value",
        "options": ["small", "large"], "required": False}]}
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)).status_code == 200

    props = (await client.get("/v1/services/nous-opt-plain/schema")).json()["input_schema"]["properties"]
    assert props["size"]["enum"] == ["small", "large"]
    assert "x-option-meta" not in props["size"]


@pytest.mark.asyncio
async def test_rich_options_keep_enum_plain_and_expose_meta(client):
    """{value,label,image} 形 options → enum 仍是纯值,元数据落 x-option-meta。"""
    tid = await _mk(client, "nous-opt-rich")
    mapping = {"exposed_params": [{
        "key": "style", "label": "风格", "type": "string",
        "comfy_node_id": "138", "comfy_input": "value", "required": False,
        "options": [
            {"value": "sai-anime", "label": "SAI-动漫", "image": "https://x/sai-anime.jpg"},
            {"value": "Fooocus Enhance", "label": "Fooocus-优化增强",
             "image": "https://x/fooocus_enhance.jpg"},
        ]}]}
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)).status_code == 200

    prop = (await client.get("/v1/services/nous-opt-rich/schema")).json()["input_schema"]["properties"]["style"]
    # enum 保持纯值 —— 校验与老前端都依赖这一点
    assert prop["enum"] == ["sai-anime", "Fooocus Enhance"]
    meta = prop["x-option-meta"]
    assert meta[0] == {"value": "sai-anime", "label": "SAI-动漫",
                       "image": "https://x/sai-anime.jpg"}
    assert meta[1]["label"] == "Fooocus-优化增强"


@pytest.mark.asyncio
async def test_rich_options_roundtrip_to_editor(client):
    """编辑器 GET 模板详情时,富选项要原样拿回来(不能被降级成裸值)。"""
    tid = await _mk(client, "nous-opt-roundtrip")
    opts = [{"value": "a", "label": "甲", "image": "https://x/a.jpg"},
            {"value": "b", "label": "乙"}]
    mapping = {"exposed_params": [{
        "key": "pick", "label": "选一个", "type": "string",
        "comfy_node_id": "138", "comfy_input": "value",
        "options": opts, "required": False}]}
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)).status_code == 200

    detail = (await client.get(f"/api/v1/comfy-templates/{tid}")).json()
    got = detail["exposed_params"][0]["options"]
    assert got == opts, got


@pytest.mark.asyncio
async def test_partial_metadata_is_allowed(client):
    """只给 value(无 label/image)的项要能和富项混排,且 enum 顺序保持。"""
    tid = await _mk(client, "nous-opt-partial")
    mapping = {"exposed_params": [{
        "key": "pick", "label": "选一个", "type": "string",
        "comfy_node_id": "138", "comfy_input": "value", "required": False,
        "options": [{"value": "a", "image": "https://x/a.jpg"}, "b",
                    {"value": "c", "label": "丙"}]}]}
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)).status_code == 200

    prop = (await client.get("/v1/services/nous-opt-partial/schema")).json()["input_schema"]["properties"]["pick"]
    assert prop["enum"] == ["a", "b", "c"]
    meta = {m["value"]: m for m in prop["x-option-meta"]}
    assert meta["a"]["image"] == "https://x/a.jpg" and "label" not in meta["a"]
    assert meta["b"] == {"value": "b"}
    assert meta["c"]["label"] == "丙"


@pytest.mark.asyncio
async def test_file_type_still_gets_no_enum_even_with_rich_options(client):
    """回归(2026-08-12 实机 400):文件类输入的 options 是 sidecar 已有文件清单,
    不是取值域。富选项形态下同样一律不写 enum,也不写 x-option-meta。"""
    tid = await _mk(client, "nous-opt-file")
    mapping = {"exposed_params": [{
        "key": "img", "label": "图", "type": "image",
        "comfy_node_id": "138", "comfy_input": "value", "required": False,
        "options": [{"value": "example.png", "image": "https://x/e.jpg"}]}]}
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)).status_code == 200

    prop = (await client.get("/v1/services/nous-opt-file/schema")).json()["input_schema"]["properties"]["img"]
    assert "enum" not in prop
    assert "x-option-meta" not in prop


@pytest.mark.asyncio
async def test_multiple_flag_surfaces_in_schema(client):
    """多选 combo(如 Krea2 的风格:逗号拼接送给 ComfyUI 的 select_styles)。

    schema 里需要一个显式信号,否则前端只能当单选渲染。值仍是 string ——
    ComfyUI-Easy-Use 的 `select_styles` 本来就吃逗号分隔字符串
    (prompt.py:196 `select_styles.split(',')`),不是数组。
    """
    tid = await _mk(client, "nous-opt-multi")
    mapping = {"exposed_params": [{
        "key": "styles", "label": "风格", "type": "string",
        "comfy_node_id": "138", "comfy_input": "value", "required": False,
        "multiple": True,
        "options": [{"value": "sai-anime", "label": "SAI-动漫", "image": "https://x/a.jpg"}]}]}
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)).status_code == 200

    prop = (await client.get("/v1/services/nous-opt-multi/schema")).json()["input_schema"]["properties"]["styles"]
    assert prop["type"] == "string", "多选仍然是逗号串,不是 array"
    assert prop["x-multiple"] is True
    # 编辑器 round-trip 也要拿得回来
    detail = (await client.get(f"/api/v1/comfy-templates/{tid}")).json()
    assert detail["exposed_params"][0]["multiple"] is True


@pytest.mark.asyncio
async def test_multiple_absent_by_default(client):
    """没声明 multiple 的字段不产生 x-multiple —— 单选是默认,老映射零变化。"""
    tid = await _mk(client, "nous-opt-single")
    mapping = {"exposed_params": [{
        "key": "pick", "label": "选", "type": "string",
        "comfy_node_id": "138", "comfy_input": "value",
        "options": ["a", "b"], "required": False}]}
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=mapping)).status_code == 200
    prop = (await client.get("/v1/services/nous-opt-single/schema")).json()["input_schema"]["properties"]["pick"]
    assert "x-multiple" not in prop


@pytest.mark.asyncio
async def test_multiple_value_passes_enum_validation(client):
    """x-multiple 的字段:逗号串里**每一项**分别比 enum,不是拿整串比。

    2026-08-31 实机:krea2 的 styles 换成 275 项枚举后,
    `{"styles": "sai-anime,Fooocus Cinematic"}` 直接 422
    "must be one of ['Fooocus Enhance', ...]" —— x-multiple 当时只影响 schema
    生成,没影响校验,于是多选值**永远过不了白名单**,功能整条是死的。
    """
    from src.services.service_schema import validate_service_input

    schema = {"type": "object", "properties": {"styles": {
        "type": "string", "enum": ["a", "b", "c"], "x-multiple": True}}}
    assert validate_service_input(schema, {"styles": "a,c"}) == []
    assert validate_service_input(schema, {"styles": "b"}) == []
    assert validate_service_input(schema, {"styles": ""}) == [], "空串=没选,合法"
    assert validate_service_input(schema, {"styles": "a, c"}) == [], "容忍空格"

    bad = validate_service_input(schema, {"styles": "a,zzz"})
    assert len(bad) == 1 and "zzz" in bad[0], bad


@pytest.mark.asyncio
async def test_single_select_still_rejects_comma_string(client):
    """回归:没有 x-multiple 的枚举字段照旧整串比对 —— 逗号串就是非法值。"""
    from src.services.service_schema import validate_service_input

    schema = {"type": "object", "properties": {"pick": {
        "type": "string", "enum": ["a", "b"]}}}
    assert validate_service_input(schema, {"pick": "a"}) == []
    assert len(validate_service_input(schema, {"pick": "a,b"})) == 1
