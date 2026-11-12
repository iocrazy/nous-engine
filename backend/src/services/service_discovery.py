"""服务发现元数据(`ServiceInstance.discovery`)的校验与公开视图 —— 唯一实现。

调用方(如 nous-app)要在**不认服务名**的前提下区分「文生图 / 图像编辑 / 图片放大」,
并知道要不要给 LoRA、要传哪些图。存的只有管理员声明、推不出来的三样:

    discovery = {"operation": ..., "display_name": ..., "lora_slots": N}

其余(`supports_lora` / `media_inputs` / `prompt_fields`)一律从 exposed_inputs 与
input schema **推导**,不重复存 —— 工作流真实参数的唯一真相仍是
`GET /v1/services/{name}/schema` 的 input_schema。公开视图里没有 ComfyUI 节点 id、
端口或模型路径。

LoRA 槽的约定:`lora_slots = N` ⇔ mapping 里恰好有 `lora_1 … lora_N` 与对应的
`lora_i_strength`,槽位 1 → N 的顺序就是加载顺序。每个 `lora_i` 必须是**封闭清单**
(options 白名单,含 `"None"` 表示空槽),清单里只能是模型目录下的相对文件名 ——
调用期由 schema 的 enum 校验兜住,任意路径进不来。
"""
from __future__ import annotations

import re
from typing import Any, Literal

from src.services.service_schema import FILE_INPUT_TYPES, input_key
from src.services.skill_run import text_input_fields

Operation = Literal["text_to_image", "image_edit", "image_upscale"]
OPERATIONS: tuple[str, ...] = ("text_to_image", "image_edit", "image_upscale")
# 这些操作的服务至少要有一张必填输入图,否则调用方按 operation 渲染的 UI 与契约对不上。
_NEEDS_MEDIA = frozenset({"image_edit", "image_upscale"})
MAX_LORA_SLOTS = 16
EMPTY_LORA = "None"  # rgthree Lora Loader Stack 的空槽取值
_NUMERIC_TYPES = frozenset({"number", "float", "int", "integer"})
_LORA_KEY_RE = re.compile(r"^lora_(\d+)$")
# 绝对路径 / 盘符 / 反斜杠 / 上跳 —— LoRA 选项只能是模型目录下的相对文件名。
_UNSAFE_OPTION_RE = re.compile(r"(^/)|(^[A-Za-z]:)|\\|(^|/)\.\.(/|$)")


def _option_values(options: Any) -> list:
    out = []
    for o in options or []:
        out.append(o["value"] if isinstance(o, dict) and "value" in o else o)
    return out


def _check_lora_slot(i: int, params: dict[str, dict]) -> None:
    key, skey = f"lora_{i}", f"lora_{i}_strength"
    lora = params.get(key)
    if lora is None:
        raise ValueError(f"discovery.lora_slots 声明了第 {i} 槽,但 mapping 里没有 {key!r}")
    values = _option_values(lora.get("options"))
    if str(lora.get("type") or "").lower() != "string" or not values:
        raise ValueError(f"{key!r} 必须是带 options 白名单的 string 参数(不接受自由路径)")
    # 这三样会让调用期校验不再按静态白名单逐值比对(多选按逗号拆开比、动态清单覆盖 enum),
    # 「封闭清单」就名不副实了。
    if lora.get("multiple") or lora.get("options_source") or lora.get("options_depends_on"):
        raise ValueError(
            f"{key!r} 不能声明 multiple / options_source / options_depends_on(LoRA 槽只收静态白名单)")
    if EMPTY_LORA not in values:
        raise ValueError(f"{key!r} 的 options 必须包含 {EMPTY_LORA!r}(空槽)")
    for v in values:
        if not isinstance(v, str) or not v or _UNSAFE_OPTION_RE.search(v):
            raise ValueError(f"{key!r} 的选项 {v!r} 不是模型目录下的相对文件名")
    if lora.get("default") not in (None, *values):
        raise ValueError(f"{key!r} 的 default {lora.get('default')!r} 不在 options 里")
    strength = params.get(skey)
    if strength is None:
        raise ValueError(f"discovery.lora_slots 声明了第 {i} 槽,但 mapping 里没有 {skey!r}")
    if str(strength.get("type") or "").lower() not in _NUMERIC_TYPES:
        raise ValueError(f"{skey!r} 必须是数值参数")
    if strength.get("min") is None or strength.get("max") is None:
        raise ValueError(f"{skey!r} 必须给出 min/max(用节点的真实约束)")


def check_discovery(discovery: dict, params: list[dict]) -> None:
    """discovery 与 mapping(平铺的 exposed_params 形)自洽,否则 ValueError。"""
    by_key = {p["key"]: p for p in params}
    op = discovery.get("operation")
    if op not in OPERATIONS:
        raise ValueError(f"discovery.operation 必须是 {list(OPERATIONS)} 之一,当前 {op!r}")
    slots = discovery.get("lora_slots", 0)
    for i in range(1, slots + 1):
        _check_lora_slot(i, by_key)
    extra = sorted(
        k for k in by_key
        if (m := _LORA_KEY_RE.match(k)) and int(m.group(1)) > slots)
    if extra:
        raise ValueError(
            f"mapping 里有 {extra},超出 discovery.lora_slots={slots};要么调大槽数,要么删掉")
    if op in _NEEDS_MEDIA and not any(
        p.get("required") and str(p.get("type") or "").lower() in FILE_INPUT_TYPES
        for p in params
    ):
        raise ValueError(f"operation={op} 的服务至少要有一个必填的图片输入")


def flatten_stored_input(item: dict) -> dict:
    """exposed_inputs 存储形(取值约束嵌在 `constraints` 里)→ check_discovery 吃的平铺形。

    给不经 comfy mapping、直接改 exposed_inputs 的写路径(`PATCH /api/v1/services/{id}`)用,
    免得那条路把 LoRA 槽改坏而已存的 discovery 还在对外声明。
    """
    c = item.get("constraints") if isinstance(item.get("constraints"), dict) else {}
    return {
        **item,
        "key": input_key(item),
        "options": c.get("option_meta") or c.get("enum") or item.get("options"),
        "min": c.get("min", item.get("min")),
        "max": c.get("max", item.get("max")),
        "multiple": c.get("multiple", item.get("multiple")),
        "options_source": c.get("options_source", item.get("options_source")),
        "options_depends_on": c.get("options_depends_on", item.get("options_depends_on")),
    }


def media_inputs(exposed_inputs: list | None) -> list[dict]:
    """文件类输入,按 mapping 顺序(图像编辑的 image → image8 顺序即参考图顺序)。"""
    out = []
    for e in exposed_inputs or []:
        t = str(e.get("type") or "").lower()
        if t in FILE_INPUT_TYPES:
            out.append({"key": input_key(e), "type": t, "required": bool(e.get("required", True))})
    return out


def discovery_view(
    discovery: dict | None, exposed_inputs: list | None, input_schema: dict,
) -> dict | None:
    """公开视图:存的三样 + 推导出的 supports_lora / media_inputs / prompt_fields。

    `prompt_fields` 与 `/v1/skill-runs/generate` 的 prompt_field 判定**同源**
    (skill_run.text_input_fields):放大这类没有自由文本字段的服务得到空列表,
    即「不是 Skill 生成目标」。
    """
    if not discovery:
        return None
    slots = int(discovery.get("lora_slots") or 0)
    return {
        "operation": discovery.get("operation"),
        "display_name": discovery.get("display_name"),
        "supports_lora": slots > 0,
        "lora_slots": slots,
        "media_inputs": media_inputs(exposed_inputs),
        "prompt_fields": text_input_fields(input_schema, exposed_inputs),
    }
