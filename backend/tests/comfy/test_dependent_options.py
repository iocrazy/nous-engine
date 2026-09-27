"""选项依赖:一个入参的选项清单由**另一个入参的当前值**决定。

动机(krea2 实机):`style_pack` 切成别的风格包后,`styles` 的缩略图网格纹丝不动 ——
注册时冻结的 275 项静态 enum 是 `fooocus_styles` 那一份,新包里的风格既渲不出来、
传上去也必被静态白名单判成 422。这里测的是通用机制(mapping → schema → 运行期校验),
不是 krea2 专用逻辑。

sidecar 一律 mock —— 测试不碰真 ComfyUI。
"""
import pytest

from src.services.comfy import style_options
from src.services.service_schema import build_service_io_schema, validate_service_input

WF = {"1243": {"class_type": "easy stylesSelector",
               "inputs": {"styles": "fooocus_styles", "select_styles": ""}}}

# 注册时冻结的静态清单(= 默认包 fooocus_styles 的那一份)。
STATIC = [{"value": "Fooocus Enhance", "label": "Fooocus-优化增强", "image": "https://x/a.jpg"},
          {"value": "sai-anime", "label": "SAI-动漫", "image": "https://x/b.jpg"}]


async def _mk(client, name):
    r = await client.post("/api/v1/comfy-templates", json={"name": name, "workflow": WF})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mapping(**overrides):
    styles = {
        "key": "styles", "label": "风格(可多选,随风格包切换)", "type": "string",
        "comfy_node_id": "1243", "comfy_input": "select_styles", "required": False,
        "multiple": True, "options": STATIC,
        "options_depends_on": "style_pack", "options_source": "comfy_styles",
    }
    styles.update(overrides)
    return {"exposed_params": [
        {"key": "style_pack", "label": "风格包", "type": "string",
         "comfy_node_id": "1243", "comfy_input": "styles", "required": False,
         "default": "fooocus_styles",
         "options": ["fooocus_styles", "krea2_397styles-anime_动漫"]},
        styles,
    ]}


class FakeClient:
    """只实现 styles();记录调用次数,用来断言 TTL 缓存真的挡住了第二次往返。"""

    def __init__(self, packs=None, boom=False):
        self.packs = packs if packs is not None else {
            "fooocus_styles": ["Fooocus Enhance", "sai-anime"],
            "krea2_397styles-anime_动漫": ["krea-cel", "krea-ink"],
        }
        self.boom = boom
        self.calls: list[str] = []
        self.timeouts: list[float] = []

    async def styles(self, pack: str, *, timeout: float = 15.0):
        self.calls.append(pack)
        self.timeouts.append(timeout)
        if self.boom:
            from src.services.comfy.client import ComfyError
            raise ComfyError("sidecar 掉线")
        return [{"name": n, "name_cn": n, "thumbnail": f"https://x/{n}.jpg"}
                for n in self.packs.get(pack, [])]


@pytest.fixture(autouse=True)
def _clear_style_cache():
    """TTL 缓存是模块级的 —— 用例之间必须清,否则顺序一变断言就飘。"""
    style_options.reset_style_options_cache()
    yield
    style_options.reset_style_options_cache()


# 全仓约定:pydantic 请求体校验失败经 main.py 的 RequestValidationError handler 统一
# 翻成 **400 + code=validation_error**(OpenAI 风格错误信封),不是裸 FastAPI 的 422。
BAD_REQUEST = 400


def _assert_rejected(r, needle: str = ""):
    assert r.status_code == BAD_REQUEST, r.text
    assert r.json()["error"]["code"] == "validation_error", r.text
    if needle:
        assert needle in r.text


# ---------- mapping 层 ----------


@pytest.mark.asyncio
async def test_mapping_round_trip(client):
    """新字段能存能取:PUT mapping → GET 模板详情原样吐回。"""
    tid = await _mk(client, "nous-dep-roundtrip")
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=_mapping())
    assert r.status_code == 200, r.text

    detail = (await client.get(f"/api/v1/comfy-templates/{tid}")).json()
    styles = next(p for p in detail["exposed_params"] if p["key"] == "styles")
    assert styles["options_depends_on"] == "style_pack"
    assert styles["options_source"] == "comfy_styles"
    assert styles["multiple"] is True
    # 静态 options 仍在(默认包的兜底),没被依赖声明顶掉
    assert styles["options"][0]["value"] == "Fooocus Enhance"
    # 不声明依赖的字段:两个键都是 None,老映射零变化
    pack = next(p for p in detail["exposed_params"] if p["key"] == "style_pack")
    assert pack["options_depends_on"] is None and pack["options_source"] is None


@pytest.mark.asyncio
async def test_depends_on_unknown_key_rejected(client):
    """指向不存在的 key → 拒绝(否则运行期静默退回静态 enum,症状查不出原因)。"""
    tid = await _mk(client, "nous-dep-unknown")
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                         json=_mapping(options_depends_on="no_such_key"))
    _assert_rejected(r, "no_such_key")


@pytest.mark.asyncio
async def test_depends_on_self_rejected(client):
    tid = await _mk(client, "nous-dep-self")
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                         json=_mapping(options_depends_on="styles"))
    _assert_rejected(r)


@pytest.mark.asyncio
async def test_depends_on_without_source_rejected(client):
    """只写 depends_on 不写 source:运行期不知道去哪儿取清单 → 发布时就拒。"""
    tid = await _mk(client, "nous-dep-nosource")
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                         json=_mapping(options_source=None))
    _assert_rejected(r)


@pytest.mark.asyncio
async def test_unknown_source_rejected(client):
    """options_source 是枚举(目前只有 comfy_styles),乱写的来源直接拒。"""
    tid = await _mk(client, "nous-dep-badsource")
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                         json=_mapping(options_source="from_mars"))
    _assert_rejected(r)


# ---------- schema 层 ----------


@pytest.mark.asyncio
async def test_schema_exposes_dependency_keys(client):
    """schema 输出 x-options-depends-on / x-options-source,静态 enum + meta 照旧。"""
    tid = await _mk(client, "nous-dep-schema")
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                             json=_mapping())).status_code == 200

    props = (await client.get("/v1/services/nous-dep-schema/schema")).json()["input_schema"]["properties"]
    prop = props["styles"]
    assert prop["x-options-depends-on"] == "style_pack"
    assert prop["x-options-source"] == "comfy_styles"
    # 兜底/离线展示的静态清单没被删
    assert prop["enum"] == ["Fooocus Enhance", "sai-anime"]
    assert prop["x-option-meta"][0]["image"] == "https://x/a.jpg"
    assert prop["x-multiple"] is True
    # 依赖参数自己不带这两个键
    assert "x-options-depends-on" not in props["style_pack"]


# ---------- 运行期校验 ----------


def _schema():
    return {"type": "object", "properties": {
        "style_pack": {"type": "string", "default": "fooocus_styles",
                       "enum": ["fooocus_styles", "krea2_397styles-anime_动漫"]},
        "styles": {"type": "string", "enum": ["Fooocus Enhance", "sai-anime"],
                   "x-multiple": True, "x-options-depends-on": "style_pack",
                   "x-options-source": "comfy_styles"}}}


@pytest.mark.asyncio
async def test_validation_allows_values_from_switched_pack(monkeypatch):
    """切到别的包后,**新包**里的风格放行 —— 静态 enum 里根本没有这些值。"""
    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    payload = {"style_pack": "krea2_397styles-anime_动漫", "styles": "krea-cel,krea-ink"}
    dyn = await style_options.resolve_dynamic_enums(_schema(), payload)
    assert dyn == {"styles": ["krea-cel", "krea-ink"]}
    assert validate_service_input(_schema(), payload, dynamic_enums=dyn) == []


@pytest.mark.asyncio
async def test_validation_rejects_values_not_in_switched_pack(monkeypatch):
    """切包后,旧包(静态 enum)里的值不再合法 —— 动态清单**覆盖**静态 enum。"""
    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    payload = {"style_pack": "krea2_397styles-anime_动漫", "styles": "sai-anime"}
    dyn = await style_options.resolve_dynamic_enums(_schema(), payload)
    errs = validate_service_input(_schema(), payload, dynamic_enums=dyn)
    assert len(errs) == 1 and "sai-anime" in errs[0], errs


@pytest.mark.asyncio
async def test_pack_falls_back_to_default_when_not_submitted(monkeypatch):
    """没传依赖参数 → 用它 schema 里的 default 当包名。"""
    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    dyn = await style_options.resolve_dynamic_enums(_schema(), {"styles": "sai-anime"})
    assert fake.calls == ["fooocus_styles"]
    assert dyn == {"styles": ["Fooocus Enhance", "sai-anime"]}


@pytest.mark.asyncio
async def test_sidecar_down_falls_back_to_static_enum(monkeypatch, caplog):
    """sidecar 不可达:不抛错,退回静态 enum 校验 + 一条 warning(预测不该 500)。"""
    fake = FakeClient(boom=True)
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    payload = {"style_pack": "krea2_397styles-anime_动漫", "styles": "sai-anime"}
    with caplog.at_level("WARNING"):
        dyn = await style_options.resolve_dynamic_enums(_schema(), payload)
    assert dyn == {}
    assert any("风格包" in r.message or "风格包" in r.getMessage() for r in caplog.records)
    # 退回静态 enum:静态清单里有的值照旧放行
    assert validate_service_input(_schema(), payload, dynamic_enums=dyn) == []


@pytest.mark.asyncio
async def test_ttl_cache_avoids_second_sidecar_call(monkeypatch):
    """同一个包第二次解析走进程内缓存,不再打 sidecar。"""
    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    payload = {"style_pack": "fooocus_styles", "styles": "sai-anime"}
    await style_options.resolve_dynamic_enums(_schema(), payload)
    await style_options.resolve_dynamic_enums(_schema(), payload)
    assert fake.calls == ["fooocus_styles"], "第二次必须命中缓存"

    # 换个包 = 换个缓存键,该打还得打
    await style_options.resolve_dynamic_enums(
        _schema(), {"style_pack": "krea2_397styles-anime_动漫", "styles": "krea-cel"})
    assert fake.calls == ["fooocus_styles", "krea2_397styles-anime_动漫"]

    # 过期后重新取
    style_options.reset_style_options_cache()
    await style_options.resolve_dynamic_enums(_schema(), payload)
    assert fake.calls.count("fooocus_styles") == 2


@pytest.mark.asyncio
async def test_no_dependency_declared_means_no_sidecar_call(monkeypatch):
    """回归:没有字段声明依赖时,预测路径一次 sidecar 都不打。"""
    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    plain = {"type": "object", "properties": {
        "pick": {"type": "string", "enum": ["a", "b"]}}}
    assert await style_options.resolve_dynamic_enums(plain, {"pick": "a"}) == {}
    assert fake.calls == []


@pytest.mark.asyncio
async def test_prediction_endpoint_uses_dynamic_pack(client, monkeypatch):
    """端到端接线:预测入口按**提交的包**校验 —— 静态 enum 里有、新包里没有的值被拒。

    故意断言"拒绝"这一侧:通过的那一侧会真的排一个渲染任务去敲 sidecar(本机可能真
    跑着 ComfyUI),测试不该碰它。放行逻辑由上面的单元用例覆盖。
    """
    tid = await _mk(client, "nous-dep-predict")
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                             json=_mapping())).status_code == 200

    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    r = await client.post(
        "/v1/services/nous-dep-predict/predictions",
        headers={"Prefer": "respond-async"},
        json={"input": {"style_pack": "krea2_397styles-anime_动漫",
                        "styles": "sai-anime"}})
    assert r.status_code == 422, r.text
    assert "sai-anime" in r.text
    assert fake.calls == ["krea2_397styles-anime_动漫"], "校验用的是提交的包,不是默认包"


# ---------- 预取的闸门与缓存(#4/#5/#6 审查项)----------


@pytest.mark.asyncio
async def test_unknown_pack_never_reaches_sidecar(monkeypatch):
    """闸门:`resolve_dynamic_enums` 跑在 `validate_service_input` **之前**,不能拿
    没经白名单的包名去打 sidecar —— 否则任何持 key 的人 POST 一串随机 pack,每次都是
    一趟 sidecar 往返 + 一个缓存条目。落不进依赖参数 enum 的值直接不取(它本来就会被
    随后的静态校验拒掉)。
    """
    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    dyn = await style_options.resolve_dynamic_enums(
        _schema(), {"style_pack": "attacker-supplied-garbage", "styles": "x"})
    assert dyn == {}
    assert fake.calls == [], "非法包名一次都不该打到 sidecar"
    assert style_options._style_cache == {}, "也不该在缓存里占一格"

    # 该值随后由静态 enum 校验拒掉 —— 没被静默放过
    errs = validate_service_input(
        _schema(), {"style_pack": "attacker-supplied-garbage", "styles": "x"})
    assert any("style_pack" in e for e in errs), errs


@pytest.mark.asyncio
async def test_dep_without_enum_only_trusts_default(monkeypatch):
    """依赖参数没声明 enum 时,只认 schema 里的 default(唯一可信的来源)。"""
    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)
    schema = _schema()
    del schema["properties"]["style_pack"]["enum"]

    assert await style_options.resolve_dynamic_enums(
        schema, {"style_pack": "whatever"}) == {}
    assert fake.calls == []

    await style_options.resolve_dynamic_enums(schema, {"style_pack": "fooocus_styles"})
    assert fake.calls == ["fooocus_styles"]


@pytest.mark.asyncio
async def test_prefetch_uses_short_timeout(monkeypatch):
    """预校验取数用 5s 短超时,不复用列清单那条的 15s —— sidecar 卡死时不该让用户
    的预测干等。"""
    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)
    await style_options.resolve_dynamic_enums(_schema(), {"style_pack": "fooocus_styles"})
    assert fake.timeouts == [style_options.STYLE_FETCH_TIMEOUT_S]
    assert style_options.STYLE_FETCH_TIMEOUT_S == 5.0


@pytest.mark.asyncio
async def test_failure_is_negatively_cached(monkeypatch):
    """失败要负缓存:sidecar 卡死时,30 秒内的后续预测直接退回静态 enum,
    不再每个都去等满超时。"""
    fake = FakeClient(boom=True)
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)
    payload = {"style_pack": "fooocus_styles", "styles": "sai-anime"}

    assert await style_options.resolve_dynamic_enums(_schema(), payload) == {}
    assert await style_options.resolve_dynamic_enums(_schema(), payload) == {}
    assert fake.calls == ["fooocus_styles"], "第二次要命中负缓存"

    # 负缓存 30 秒(远短于成功的 10 分钟)—— sidecar 一恢复就能自愈
    assert style_options.STYLE_CACHE_FAILURE_TTL_S == 30.0
    assert style_options.STYLE_CACHE_FAILURE_TTL_S < style_options.STYLE_CACHE_TTL_S


@pytest.mark.asyncio
async def test_failure_negative_cache_expires(monkeypatch):
    """负缓存过期后重新尝试(不是永久放弃)。"""
    fake = FakeClient(boom=True)
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)
    await style_options.style_values("fooocus_styles")
    assert fake.calls == ["fooocus_styles"]

    # 把那条负缓存的时间戳往前拨过 TTL
    ts, values = style_options._style_cache["fooocus_styles"]
    style_options._style_cache["fooocus_styles"] = (
        ts - style_options.STYLE_CACHE_FAILURE_TTL_S - 1, values)

    fake.boom = False
    assert await style_options.style_values("fooocus_styles") == [
        "Fooocus Enhance", "sai-anime"]
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_empty_pack_is_empty_whitelist_not_static_fallback(monkeypatch):
    """**取到了但是空** ≠ **取不到**。

    包存在却一个风格都没有(没加载)时退回默认包的静态 enum,正好是本机制要防的
    "拿 A 包的清单校验 B 包"。空清单 = 空白名单,全拒,且错误信息要说清是清单空。
    """
    fake = FakeClient(packs={"krea2_397styles-anime_动漫": []})
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    payload = {"style_pack": "krea2_397styles-anime_动漫", "styles": "sai-anime"}
    dyn = await style_options.resolve_dynamic_enums(_schema(), payload)
    assert dyn == {"styles": []}, "空清单要进结果(空白名单),不是当没拿到"

    errs = validate_service_input(_schema(), payload, dynamic_enums=dyn)
    assert len(errs) == 1 and "清单为空" in errs[0], errs
    # 没选任何风格时不该报错(空串 = 没选)
    assert validate_service_input(
        _schema(), {**payload, "styles": ""}, dynamic_enums=dyn) == []


@pytest.mark.asyncio
async def test_empty_result_cached_only_briefly(monkeypatch):
    """空结果按负缓存的 30 秒算,不占满 10 分钟 —— 包加载完就该自愈。"""
    fake = FakeClient(packs={"fooocus_styles": []})
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)
    assert await style_options.style_values("fooocus_styles") == []

    ts, values = style_options._style_cache["fooocus_styles"]
    style_options._style_cache["fooocus_styles"] = (
        ts - style_options.STYLE_CACHE_FAILURE_TTL_S - 1, values)
    fake.packs = {"fooocus_styles": ["Fooocus Enhance"]}
    assert await style_options.style_values("fooocus_styles") == ["Fooocus Enhance"]


@pytest.mark.asyncio
async def test_cache_is_bounded(monkeypatch):
    """缓存条目有上限并按插入序驱逐 —— 包名来自请求,不设上限就是个能被喂大的字典。"""
    packs = {f"pack-{i}": [f"style-{i}"] for i in range(
        style_options.STYLE_CACHE_MAX_ENTRIES + 20)}
    fake = FakeClient(packs=packs)
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    for name in packs:
        await style_options.style_values(name)

    assert len(style_options._style_cache) == style_options.STYLE_CACHE_MAX_ENTRIES
    assert "pack-0" not in style_options._style_cache, "最旧的被驱逐"
    assert f"pack-{len(packs) - 1}" in style_options._style_cache, "最新的还在"


@pytest.mark.asyncio
async def test_cache_refresh_moves_entry_to_newest(monkeypatch):
    """刷新已有 key 要把它挪到队尾,免得热条目因为当初插得早而被优先驱逐。"""
    fake = FakeClient(packs={"a": ["x"], "b": ["y"]})
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)
    await style_options.style_values("a")
    await style_options.style_values("b")
    style_options._cache_put("a", ["x"])
    assert list(style_options._style_cache) == ["b", "a"]


# ---------- 不冻结静态 options、只声明依赖(enum-less mapping)----------
#
# krea2 的 mapping 是「静态 options + 依赖」,下面这批覆盖的是另一种合法形状:
# **一项静态 options 都不冻结**,值域完全交给运行期的动态清单。三处只在这种形状下
# 才触发的 bug(multiple 丢失 / 前端渲成文本框 / 文件类被套清单)就是从这儿进来的。


def _schema_no_static_enum():
    """enum-less 的 styles:只有 x-multiple + 依赖声明,没有冻结的静态 enum。"""
    return {"type": "object", "properties": {
        "style_pack": {"type": "string", "default": "fooocus_styles",
                       "enum": ["fooocus_styles", "krea2_397styles-anime_动漫"]},
        "styles": {"type": "string", "x-multiple": True,
                   "x-options-depends-on": "style_pack",
                   "x-options-source": "comfy_styles"}}}


@pytest.mark.asyncio
async def test_enum_less_mapping_keeps_multiple_flag(client):
    """`multiple` 是**值的形状**标志,跟有没有冻结静态 enum 无关。

    曾经它被嵌在 `if m.options` 里 —— 不冻结 options 的 mapping 存不下这个标志,
    schema 没有 x-multiple,运行期就拿动态清单整串比对 "a,b" → 多选必 422。
    """
    tid = await _mk(client, "nous-dep-enumless")
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                         json=_mapping(options=None))
    assert r.status_code == 200, r.text

    detail = (await client.get(f"/api/v1/comfy-templates/{tid}")).json()
    styles = next(p for p in detail["exposed_params"] if p["key"] == "styles")
    assert styles["multiple"] is True

    prop = (await client.get("/v1/services/nous-dep-enumless/schema")
            ).json()["input_schema"]["properties"]["styles"]
    assert prop["x-multiple"] is True, "没有它,多选值会被整串比对"
    assert "enum" not in prop, "本来就没冻结静态清单"
    assert prop["x-options-depends-on"] == "style_pack"
    assert prop["x-options-source"] == "comfy_styles"


@pytest.mark.asyncio
async def test_enum_less_multi_select_validates_per_item(monkeypatch):
    """enum-less + 多选:逗号串按动态清单**逐项**校验,整串放行。"""
    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    schema = _schema_no_static_enum()
    payload = {"style_pack": "krea2_397styles-anime_动漫", "styles": "krea-cel,krea-ink"}
    dyn = await style_options.resolve_dynamic_enums(schema, payload)
    assert dyn == {"styles": ["krea-cel", "krea-ink"]}
    assert validate_service_input(schema, payload, dynamic_enums=dyn) == []


@pytest.mark.asyncio
async def test_enum_less_multi_select_reports_only_bad_items(monkeypatch):
    """逐项校验的另一半:只有清单外的那几项进报错,合法项不受牵连。"""
    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    schema = _schema_no_static_enum()
    payload = {"style_pack": "krea2_397styles-anime_动漫", "styles": "krea-cel,not-a-style"}
    dyn = await style_options.resolve_dynamic_enums(schema, payload)
    errs = validate_service_input(schema, payload, dynamic_enums=dyn)
    assert len(errs) == 1 and "not-a-style" in errs[0], errs
    assert "krea-cel,not-a-style" not in errs[0], "整串比对的老症状"


@pytest.mark.asyncio
async def test_enum_less_prediction_accepts_multi_select(client, monkeypatch):
    """端到端:enum-less mapping 下,多选串里只有非法项被点名(不是整串被拒)。

    跟 test_prediction_endpoint_uses_dynamic_pack 同理只断言"拒绝"这一侧 —— 放行会
    真排一个渲染任务去敲 sidecar。合法项没进报错,就证明走的是逐项分支。
    """
    tid = await _mk(client, "nous-dep-enumless-predict")
    assert (await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                             json=_mapping(options=None))).status_code == 200

    fake = FakeClient()
    monkeypatch.setattr(style_options, "get_comfy_client", lambda: fake)

    r = await client.post(
        "/v1/services/nous-dep-enumless-predict/predictions",
        headers={"Prefer": "respond-async"},
        json={"input": {"style_pack": "krea2_397styles-anime_动漫",
                        "styles": "krea-cel,not-a-style"}})
    assert r.status_code == 422, r.text
    assert "not-a-style" in r.text
    assert "not in allowed values" in r.text, "整串比对会是 must be one of"


# ---------- 文件类参数不接受选项依赖 ----------


def _file_mapping(**overrides):
    """一个文件类入参(上传图)+ 依赖声明 —— 本来就不该被接受的形状。"""
    portrait = {
        "key": "portrait", "label": "参考图", "type": "image",
        "comfy_node_id": "1243", "comfy_input": "select_styles", "required": False,
        "options_depends_on": "style_pack", "options_source": "comfy_styles",
    }
    portrait.update(overrides)
    return {"exposed_params": [
        {"key": "style_pack", "label": "风格包", "type": "string",
         "comfy_node_id": "1243", "comfy_input": "styles", "required": False,
         "default": "fooocus_styles",
         "options": ["fooocus_styles", "krea2_397styles-anime_动漫"]},
        portrait,
    ]}


@pytest.mark.asyncio
async def test_file_param_rejects_options_dependency(client):
    """文件类字段的值是**上传的文件**,不是从清单里选一项。给它挂动态清单 = 运行期拿
    一份风格名当白名单,上传必 422(2026-08-12 那个静态 enum 回归换条路复现)。
    """
    tid = await _mk(client, "nous-dep-file")
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping", json=_file_mapping())
    _assert_rejected(r, "文件类参数")


@pytest.mark.asyncio
async def test_file_param_rejects_bare_options_source(client):
    """只写 options_source 不写 depends_on 也拒 —— 它一样会让 schema 挂上动态清单。"""
    tid = await _mk(client, "nous-dep-file-src")
    r = await client.put(f"/api/v1/comfy-templates/{tid}/mapping",
                         json=_file_mapping(options_depends_on=None))
    _assert_rejected(r, "文件类参数")


@pytest.mark.parametrize("ftype", ["image", "file", "audio", "video", "binary", "media"])
def test_file_param_schema_never_carries_dynamic_options(ftype):
    """双保险(老数据 / 绕过 MappingBody 的路径):schema 侧对文件类也不输出
    x-options-*,跟不输出 enum 同理 —— 有它 resolve_dynamic_enums 就会给上传字段
    发一份风格清单当白名单。
    """
    exposed = [{
        "key": "portrait", "node_id": "bridge", "input_name": "portrait", "type": ftype,
        "constraints": {"enum": ["5 (1).jpg", "example.png"],
                        "options_depends_on": "style_pack",
                        "options_source": "comfy_styles"},
    }]
    prop = build_service_io_schema(
        exposed, [], {"nodes": [], "edges": []})["input_schema"]["properties"]["portrait"]
    assert "enum" not in prop
    assert "x-options-depends-on" not in prop
    assert "x-options-source" not in prop


@pytest.mark.asyncio
async def test_file_param_upload_survives_dynamic_resolution():
    """接上一条的后果面:这样的 schema 过 resolve_dynamic_enums 拿不到任何清单,
    上传的 data URI 因此不会撞白名单(不然就是 2026-08-12 那个上传必 400)。"""
    exposed = [{
        "key": "portrait", "node_id": "bridge", "input_name": "portrait", "type": "image",
        "constraints": {"options_depends_on": "style_pack", "options_source": "comfy_styles"},
    }]
    schema = build_service_io_schema(exposed, [], {"nodes": [], "edges": []})["input_schema"]
    payload = {"portrait": "data:image/png;base64,AAAA"}
    dyn = await style_options.resolve_dynamic_enums(schema, payload)
    assert dyn == {}
    assert validate_service_input(schema, payload, dynamic_enums=dyn) == []
