# 训练工坊 · Qwen-Image-2.1

从控制台侧栏打开「训练工坊」。数据集支持本地图片、同名 TXT 与图库素材。训练使用提交时的图片顺序和描述快照。后台任务有独立的记录、日志、Loss、取消和快照重试。使用同一提示词和种子生成底模与 LoRA 样图，再将选定 checkpoint 发布到 ComfyUI 的 `qwen2.1/`。

## 主机配置

| 环境变量 | 用途 |
| --- | --- |
| `NOUS_TRAINING_DATA_DIR` | 可写的数据集、训练日志、样图和 checkpoint 存储目录。默认 `~/.local/share/nous-engine/training`。 |
| `NOUS_TRAINING_MODEL_ROOT` | 可写的训练模型根目录。工坊扫描 `Qwen/Qwen-Image-2.1/`，也识别根目录本身的官方组件布局。 |
| `NOUS_TRAINING_DIFFSYNTH_DIR` | DiffSynth-Studio Git 检出，固定在 `4f67cdf053341ee19a1995b99f3edce76ec11d1c`。在该目录的独立 `.venv` 安装 DiffSynth 与 CUDA 版 PyTorch，保留官方训练脚本。 |
| `NOUS_TRAINING_GPU_UUID` | 独立训练卡完整 UUID。首期配方以 40 GiB 为下限，等待显存空闲后启动。训练和预览共享串行队列。避免把用于推理或 ComfyUI 渲染的卡设为训练卡。 |
| `NOUS_TRAINING_LORA_ROOT` | 可写的 ComfyUI LoRA 根目录，用于发布与已有 LoRA 目录同步。 |
| `NOUS_TRAINING_CAPTION_MODEL` | 可选的视觉模型标识，使用主机既有 `VLLM_BASE_URL`。启用后可批量标注、显式覆盖和逐项重试。 |

模型下载支持魔搭和 Hugging Face。下载前显示欠缺组件和可用空间。断点数据位于模型根目录的 `.nous-training-download/`。下载文件通过配方 SHA-256 验证后逐个登记。已存在的文件会按组件路径和精确大小匹配。

已生成的 LoRA 预览由 DiffSynth 试载。发布后，工坊查询 ComfyUI 模型列表，确认文件是否被枚举。实际 ComfyUI 工作流试载属于 GPU 验收。

## 验收

先在「模型与目录」检查模型、存储、训练环境和独立 GPU。使用少量样本与一轮训练验证任务、checkpoint、固定参数预览和 LoRA 发布，再在目标 ComfyUI 服务运行一次试载。同名发布保留现有文件，需另选名称。训练进程因服务停止而中断时，任务可用原始快照重试。

[设计与原型](superpowers/specs/2026-11-20-training-studio-design.md) · [DiffSynth 官方 Qwen 训练说明](https://github.com/modelscope/DiffSynth-Studio/blob/4f67cdf053341ee19a1995b99f3edce76ec11d1c/docs/en/Model_Details/Qwen-Image-2.1.md)
