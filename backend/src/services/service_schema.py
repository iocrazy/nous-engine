"""Per-service I/O JSON-Schema 生成 + 调用期校验(服务层 API spec 2026-06-03,PR-1)。

发布的 ServiceInstance 的 `exposed_inputs/outputs`(元数据)+ 各节点 node.yaml `widgets` 定义
→ per-service JSON-Schema(input/output)。机器可发现(`GET /services/{name}/schema`)+ 调用期校验。
对齐 Cog「声明即 schema」+ ComfyUI object_info(节点 widget 即类型源)。**无 jsonschema 依赖,手写校验。**

PR-1 只生成 schema + 校验函数 + 端点;把校验**接进调用路径**是 PR-2(统一 prediction 端点)的事。
"""
from __future__ import annotations

from typing import Any

# node.yaml widget → JSON-Schema base type(select/slider 另行处理)。
_WIDGET_BASE_TYPE = {"slider": "number", "checkbox": "boolean", "seed": "integer"}
# ExposedParam.type 粗类型 → JSON-Schema type。
_EXPOSED_TYPE = {
    "int": "integer", "integer": "integer", "float": "number", "number": "number",
    "bool": "boolean", "boolean": "boolean", "string": "string",
    "object": "object", "array": "array",
}
# 输出里这些被当作「文件/产物」→ string + format=uri(交付契约 PR-4 落实)。
_FILE_OUT_TYPES = {"image", "file", "audio", "video", "latent"}
# 输入里这些是「上传一个文件」,不是从固定清单里选(见 _input_property 的 enum 说明)。
_FILE_IN_TYPES = {"image", "file", "audio", "video", "binary", "media"}


def _node_class_map(snapshot: Any) -> dict[str, str]:
    """node_id → class_type。兼容 snapshot 两种形:{"nodes": {id: {...}}} 或 {"nodes": [{id,...}]}。"""
    out: dict[str, str] = {}
    nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else None
    if isinstance(nodes, dict):
        for nid, node in nodes.items():
            if isinstance(node, dict):
                out[str(nid)] = node.get("class_type") or node.get("type")
    elif isinstance(nodes, list):
        for n in nodes:
            if isinstance(n, dict) and n.get("id") is not None:
                out[str(n.get("id"))] = n.get("class_type") or n.get("type")
    return out


def _widget_index(class_type: str | None) -> dict[str, dict]:
    """node 类型的 widgets → {name: widget_def}。拿不到定义/无 widgets → 空(回退 ExposedParam.type)。"""
    if not class_type:
        return {}
    try:
        from nodes import get_all_definitions  # noqa: PLC0415
        defn = get_all_definitions().get(class_type) or {}
    except Exception:  # noqa: BLE001 — 定义加载失败不该拖垮 schema 生成
        return {}
    out: dict[str, dict] = {}
    for w in (defn.get("widgets") or []):
        if isinstance(w, dict) and w.get("name"):
            out[w["name"]] = w
    return out


def _option_values(options: Any) -> list:
    """node.yaml options(['a','b'] 或 [{value,label,description}])→ 值列表。"""
    out = []
    for o in options or []:
        if isinstance(o, dict):
            if "value" in o:
                out.append(o["value"])
        else:
            out.append(o)
    return out


def _all_numeric(values: list) -> bool:
    return bool(values) and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)


def _input_property(exposed: dict, widget: dict | None) -> dict:
    """一个 exposed_input(+ 可选 widget 定义)→ JSON-Schema property。"""
    prop: dict[str, Any] = {}
    wtype = (widget or {}).get("widget")
    if wtype == "select":
        enum = _option_values((widget or {}).get("options"))
        if enum:
            prop["enum"] = enum
        prop["type"] = "number" if _all_numeric(enum) else "string"
    elif wtype in _WIDGET_BASE_TYPE:
        prop["type"] = _WIDGET_BASE_TYPE[wtype]
        if wtype == "slider":
            if (widget or {}).get("min") is not None:
                prop["minimum"] = widget["min"]
            if (widget or {}).get("max") is not None:
                prop["maximum"] = widget["max"]
    else:
        # 无 widget / 文本类:回退 ExposedParam.type。
        t = str(exposed.get("type") or "string").lower()
        prop["type"] = _EXPOSED_TYPE.get(t, "string")
    # I3 fix:exposed_input 自带的 `constraints`(comfy_templates.py 桥映射写入的
    # min/max/step/enum/random,见 _mapping_to_exposed_input)—— 独立于上面的 node.yaml
    # widget 分支:桥节点 class_type 是 "comfyui_workflow",从来没有 widgets 定义,
    # widget 分支永远走不到,min/max/enum 不接进这里就丢了。constraints 存在时覆盖/
    # 补齐(不是二选一——widget 分支给的 type 仍然生效,这里只加 enum/minimum/maximum)。
    constraints = exposed.get("constraints")
    if isinstance(constraints, dict):
        is_file = str(exposed.get("type") or "").lower() in _FILE_IN_TYPES
        enum = constraints.get("enum")
        # 文件类输入的 enum 不是取值域,是 **sidecar 上已有文件的清单**(ComfyUI
        # object_info 给 LoadImage.image 的就是 input/ 目录列表)。当白名单校验会
        # 把"上传一个新文件"这件正事判成非法(2026-08-12 实机:上传图片必 400
        # "must be one of ['5 (1).jpg','example.png']")。文件类一律不写 enum。
        if enum and not is_file:
            prop["enum"] = enum
            # 每选项的显示名 + 缩略图(comfy_templates._split_options 写入)。放同级扩展
            # 关键字而不是塞进 enum —— enum 必须保持纯值列表,校验和旧前端都依赖它。
            # 前端有这个键就渲缩略图网格,没有就退回原来的 <select>。
            option_meta = constraints.get("option_meta")
            if option_meta:
                prop["x-option-meta"] = option_meta
        # 多选信号:值仍是 string(逗号串),只是允许选多项。**独立于静态 enum 输出** ——
        # 只声明动态依赖、没冻结 enum 的字段同样要带上,否则 validate_service_input 会拿
        # 动态清单去整串比对 "a,b" 而不是逐项比,多选必 422。
        if constraints.get("multiple"):
            prop["x-multiple"] = True
        # 选项依赖(与静态 enum 并存,不是二选一):值域取决于另一个入参的当前值
        # (`x-options-depends-on` 指向那个入参的 key),运行期去
        # `x-options-source` 指定的来源取清单。上面的静态 enum / x-option-meta
        # 照旧输出 —— 它们是**默认包**那一份,当来源不可达时的兜底与离线展示。
        # 文件类字段一律不输出:它的值是上传的文件,给它挂一份动态清单会让
        # resolve_dynamic_enums 拿风格名当白名单,上传必 422(与上面不写 enum 同理)。
        if not is_file:
            if constraints.get("options_depends_on"):
                prop["x-options-depends-on"] = constraints["options_depends_on"]
            if constraints.get("options_source"):
                prop["x-options-source"] = constraints["options_source"]
        if constraints.get("min") is not None:
            prop["minimum"] = constraints["min"]
        if constraints.get("max") is not None:
            prop["maximum"] = constraints["max"]
    # default:exposed 优先,其次 widget。
    default = exposed.get("default")
    if default is None and widget is not None:
        default = widget.get("default")
    if default is not None:
        prop["default"] = default
    if exposed.get("label"):
        prop["description"] = exposed["label"]
    return prop


def _input_key(exposed: dict) -> str | None:
    return exposed.get("key") or exposed.get("api_name") or exposed.get("input_name") or exposed.get("param_key")


def build_service_io_schema(exposed_inputs, exposed_outputs, snapshot) -> dict:
    """→ {"input_schema": <JSON-Schema obj>, "output_schema": <JSON-Schema obj>}。"""
    cmap = _node_class_map(snapshot or {})

    in_props: dict[str, dict] = {}
    required: list[str] = []
    for raw in (exposed_inputs or []):
        e = dict(raw)
        key = _input_key(e)
        if not key:
            continue
        ct = cmap.get(str(e.get("node_id")))
        widget = _widget_index(ct).get(e.get("input_name") or e.get("param_key"))
        in_props[key] = _input_property(e, widget)
        if e.get("required", True):
            required.append(key)
    input_schema: dict[str, Any] = {"type": "object", "properties": in_props}
    if required:
        input_schema["required"] = required

    out_props: dict[str, dict] = {}
    for raw in (exposed_outputs or []):
        e = dict(raw)
        key = e.get("key") or e.get("api_name") or e.get("input_name")
        if not key:
            continue
        t = str(e.get("type") or "string").lower()
        if t in _FILE_OUT_TYPES:
            prop: dict[str, Any] = {"type": "string", "format": "uri"}
        else:
            prop = {"type": _EXPOSED_TYPE.get(t, "string")}
        if e.get("label"):
            prop["description"] = e["label"]
        out_props[key] = prop
    output_schema = {"type": "object", "properties": out_props}

    return {"input_schema": input_schema, "output_schema": output_schema}


def _enum_hint(values: list) -> str:
    """错误信息里的允许值清单 —— 动态清单可能有几百项,截断到前 20。"""
    if len(values) <= 20:
        return str(values)
    return f"{values[:20]}…(共 {len(values)} 项)"


def validate_service_input(
    input_schema: dict, payload: Any, dynamic_enums: dict[str, list] | None = None,
) -> list[str]:
    """手写校验(无 jsonschema 依赖):required / type / enum / min-max。返回错误列表(空=通过)。

    多余的未声明字段放过(passthrough,不报错)—— 工作流可能有 schema 没覆盖的内部入参。

    `dynamic_enums`:`{key: 允许值列表}`,**覆盖**该字段 schema 里的静态 enum。给带
    `x-options-source` 的字段用(值域随另一个入参变,见 `comfy/style_options.py`)。
    本函数保持同步无 I/O —— 清单由调用方预取后传进来(理由见 resolve_dynamic_enums 的
    docstring)。传 None / 该 key 不在里面 → 照旧用静态 enum。
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["input must be a JSON object"]
    props = (input_schema or {}).get("properties") or {}
    for k in (input_schema or {}).get("required") or []:
        if payload.get(k) is None:
            errors.append(f"missing required input: {k}")
    for k, v in payload.items():
        spec = props.get(k)
        if spec is None or v is None:
            continue
        t = spec.get("type")
        if t == "string" and not isinstance(v, str):
            errors.append(f"{k}: expected string")
        elif t == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
            errors.append(f"{k}: expected integer")
        elif t == "number" and (not isinstance(v, (int, float)) or isinstance(v, bool)):
            errors.append(f"{k}: expected number")
        elif t == "boolean" and not isinstance(v, bool):
            errors.append(f"{k}: expected boolean")
        # 动态清单优先于静态 enum;两者都没有就跳过白名单校验(如自由文本字段,
        # 或声明了动态来源但来源不可达且当初没冻结静态 enum 的字段)。
        allowed = (dynamic_enums or {}).get(k)
        if allowed is None:
            allowed = spec.get("enum")
        if allowed == [] and dynamic_enums and k in dynamic_enums:
            # 来源答了、但这个包一个选项都没有(包空 / 没加载)。**不能**退回静态 enum
            # ——那是默认包的清单,拿它放行正是本机制要防的错配。空白名单 = 全拒,
            # 但错误信息得说清楚是"清单空",而不是干巴巴甩一个 `[]`。
            if not (spec.get("x-multiple") and isinstance(v, str) and not v.strip()):
                errors.append(
                    f"{k}: 选项清单为空(依赖参数指向的来源当前没有可选值,"
                    f"可能是该包未加载),暂不接受任何值")
        elif allowed is not None:
            # x-multiple 的字段传的是**逗号分隔串**(对齐 ComfyUI-Easy-Use 的
            # select_styles,prompt.py:196 `.split(',')`),要逐项比对而不是整串比 ——
            # 否则多选值永远撞不上白名单,功能整条是死的(2026-08-31 实机 422)。
            if spec.get("x-multiple") and isinstance(v, str):
                picked = [x.strip() for x in v.split(",") if x.strip()]
                unknown = [x for x in picked if x not in allowed]
                if unknown:
                    errors.append(
                        f"{k}: {unknown} not in allowed values {_enum_hint(allowed)}")
            elif v not in allowed:
                errors.append(f"{k}: must be one of {_enum_hint(allowed)}")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "minimum" in spec and v < spec["minimum"]:
                errors.append(f"{k}: must be >= {spec['minimum']}")
            if "maximum" in spec and v > spec["maximum"]:
                errors.append(f"{k}: must be <= {spec['maximum']}")
    return errors


# 公开别名:skill_run 等调用方用,不直接 import 私有名。
FILE_INPUT_TYPES = _FILE_IN_TYPES
input_key = _input_key
