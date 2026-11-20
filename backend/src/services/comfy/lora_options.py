import re
from collections.abc import Sequence
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from src.services.comfy.client import get_comfy_client

LORA_LIST_TYPE = "lora_list"
POWER_LORA_LOADER = "Power Lora Loader (rgthree)"


class LoraSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    lora: Annotated[str, Field(min_length=1)]
    strength: Annotated[float, Field(ge=-10, le=10)]
    on: bool


LORA_LIST = TypeAdapter(list[LoraSelection])
_SLOT_KEY = re.compile(r"^(?:(?:qwen|moss|oldx)_lora_|lora_\d|strength_\d|oldx_strength$)")
_MODEL_DIRECTORIES = {"qwen_loras": "qwen2.1/", "edit_loras": "qwen2.1/", "oldx_loras": "qwen2.1/",
                      "moss_loras": "moss/"}


def lora_matches_input(key: str, filename: str) -> bool:
    directory = _MODEL_DIRECTORIES.get(key)
    return directory is None or filename.casefold().startswith(directory)


def validate_lora_inputs(keys: Sequence[str], payload: dict) -> list[str]:
    if not keys:
        return []
    errors = [f"{key}: please use a LoRA list instead" for key in payload if _SLOT_KEY.match(key)]
    for key in keys:
        if key not in payload:
            continue
        try:
            rows = LORA_LIST.validate_python(payload[key])
        except ValidationError as exc:
            for issue in exc.errors(include_url=False):
                path = ".".join(map(str, (key, *issue["loc"])))
                errors.append(f"{path}: {issue['msg']}")
            continue
        directory = _MODEL_DIRECTORIES.get(key)
        if directory:
            errors.extend(f"{key}.{index}.lora: 文件必须位于 {directory}"
                          for index, row in enumerate(rows) if row.on and not lora_matches_input(key, row.lora))
    return errors


async def lora_files(client: Any = None) -> list[str]:
    client = client if client is not None else get_comfy_client()
    info = await client.node_info("LoraLoader")
    declaration = (info or {}).get("input", {}).get("required", {}).get("lora_name")
    if not isinstance(declaration, list) or not declaration or not isinstance(declaration[0], list):
        raise ValueError("无法读取 ComfyUI LoRA 文件清单")
    return [name for name in declaration[0] if isinstance(name, str)]


def replace_power_loras(node: dict, rows: list[LoraSelection]) -> None:
    inputs = node["inputs"]
    for key in tuple(inputs):
        if key.lower().startswith("lora_"):
            del inputs[key]
    inputs.update({f"lora_{index}": row.model_dump() for index, row in enumerate(rows, 1)})
