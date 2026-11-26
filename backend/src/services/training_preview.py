"""Entry point for the version-pinned DiffSynth environment."""

import argparse
import json
from pathlib import Path

LORA_A = ".lora_A.weight"
LORA_B = ".lora_B.weight"


def validate_lora_targets(module, weights) -> int:
    targets = dict(module.named_modules())
    names = {key.removesuffix(LORA_A) for key in weights if key.endswith(LORA_A)}
    names.update(key.removesuffix(LORA_B) for key in weights if key.endswith(LORA_B))
    if not names:
        raise ValueError("Checkpoint 未包含可试载的 LoRA 参数")

    for name in sorted(names):
        down, up = weights.get(name + LORA_A), weights.get(name + LORA_B)
        base = getattr(targets.get(name), "weight", None)
        if down is None or up is None or base is None:
            raise ValueError(f"LoRA 参数与底模不匹配：{name}")
        if len(down.shape) != 2 or len(up.shape) != 2:
            raise ValueError(f"LoRA 参数维度无效：{name}")
        if down.shape[0] <= 0 or down.shape[0] != up.shape[1] or (up.shape[0], down.shape[1]) != tuple(base.shape):
            raise ValueError(f"LoRA 参数尺寸与底模不匹配：{name}")
    return len(names)


def main() -> None:
    import torch
    from diffsynth.pipelines.qwen_image_21 import ModelConfig, QwenImage21Pipeline
    from safetensors.torch import load_file

    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    config = json.loads(parser.parse_args().config.read_text(encoding="utf-8"))
    output = Path(config["output"])
    pipe = QwenImage21Pipeline.from_pretrained(
        torch_dtype=torch.bfloat16, device="cuda",
        model_configs=[ModelConfig(path=path) for path in config["weights"]],
        processor_config=ModelConfig(path=config["processor"]),
    )
    checkpoint = load_file(config["checkpoint"], device="cpu")
    loader = pipe.lora_loader(device=pipe.device, torch_dtype=pipe.torch_dtype)
    try:
        converted = loader.convert_state_dict(checkpoint)
    except KeyError as exc:
        raise ValueError("Checkpoint 的 LoRA 权重不完整") from exc
    matched = validate_lora_targets(pipe.dit, converted)
    print(f"已验证 {matched} 个 LoRA 参数目标与底模匹配")
    arguments = {
        "seed": config["seed"],
        "width": config["size"],
        "height": config["size"],
        "num_inference_steps": config["steps"],
    }
    pipe(config["prompt"], **arguments).save(output / "base.png")
    pipe.load_lora(pipe.dit, state_dict=converted, hotload=False)
    pipe(config["prompt"], **arguments).save(output / "lora.png")


if __name__ == "__main__":
    main()
