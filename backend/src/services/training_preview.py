"""Entry point for the version-pinned DiffSynth environment."""

import argparse
import json
from pathlib import Path

import torch
from diffsynth.pipelines.qwen_image_21 import ModelConfig, QwenImage21Pipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    config = json.loads(parser.parse_args().config.read_text(encoding="utf-8"))
    output = Path(config["output"])
    pipe = QwenImage21Pipeline.from_pretrained(
        torch_dtype=torch.bfloat16, device="cuda",
        model_configs=[ModelConfig(path=path) for path in config["weights"]],
        processor_config=ModelConfig(path=config["processor"]),
    )
    arguments = {
        "seed": config["seed"],
        "width": config["size"],
        "height": config["size"],
        "num_inference_steps": config["steps"],
    }
    pipe(config["prompt"], **arguments).save(output / "base.png")
    pipe.load_lora(pipe.dit, config["checkpoint"])
    pipe(config["prompt"], **arguments).save(output / "lora.png")


if __name__ == "__main__":
    main()
