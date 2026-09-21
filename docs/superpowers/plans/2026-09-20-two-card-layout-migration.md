# 两卡布局迁移实施计划（Pro 6000 + Pro 5000）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal：** 把 nous-engine 从「Pro 6000 + 2×RTX 3090」三卡布局迁到「Pro 6000 + Pro 5000」两卡布局 —— Pro 6000 清空独占给 ComfyUI，Pro 5000 承载 LLM / ASR / embedding / OCR。

**Architecture：** 两张卡按**职责**分工而非叠加（异构卡不能做张量并行，`topology.validate_gpu_group` 硬性要求同型号）。Pro 6000（95.6 GiB）零常驻，整块留给 ComfyUI 的出图出片；Pro 5000（71.7 GiB）跑全部推理服务。因为常驻集合能否装下取决于**每个模型的真实显存地板**，而 OCR 的地板从未实测过，本计划**先测后改**：Task 3 量出真实数字，Task 4 才据此定常驻名单。

**Tech Stack：** vLLM 0.28（LLM / embedding / OCR）、SGLang Omni（MOSS ASR，独立 venv）、ComfyUI sidecar、Postgres `model_runtime_overrides`。

**Spec：** 无独立 spec 文档。依据是本计划「## 依据：实测数据」一节 —— 全部来自 2026-09-15~20 本机 nvidia-smi 与 vLLM 启动日志，不是声明值。

## Global Constraints

- **异构卡不能组张量并行**：`backend/src/gpu/topology.py:269` 的 `validate_gpu_group` 对型号不一致直接返回 error。Pro 5000 + Pro 6000 永远不能进同一个 group。
- **`gpu_memory_utilization`（vLLM）和 `mem_fraction_static`（SGLang）都是「占整卡容量的比例」**，不是绝对值。换卡必须重算：同一个 0.26 在 95.6 GiB 卡上是 24.7 GiB，在 71.7 GiB 卡上是 18.6 GiB。
- **同卡上多个 vLLM 实例的 util 之和必须 < 1.0**（还要留 CUDA context，每进程 ~0.5–1 GiB）。
- **`qwen3_8_27b_abliterated_awq` 永不搬到 Pro 6000**（用户 2026-09-15 决定）。本计划把它放 Pro 5000，符合该约束。
- **DB `model_runtime_overrides` 优先级高于 yaml**。改 yaml 不清 override 等于没改（2026-09-15 真机踩过）。
- **`DEFAULT_RESERVED_GB = 4.0`**（`backend/src/services/gpu_monitor.py:12`），`test_resident_capacity.py` 按它兜常驻容量。
- **数据面不懒加载**：未加载模型即刻 503 `model_not_ready`。非常驻模型调用前必须手动加载。
- 每个 Task 结束时全量测试须绿：`cd backend && uv run pytest tests -n 8 -q --ignore=tests/integration`。

## 依据：实测数据

| 项 | 数值 | 来源 |
|---|---|---|
| Pro 5000 容量 | 73415 MiB = **71.7 GiB** | `nvidia-smi` 2026-09-20 |
| Pro 6000 容量 | 97887 MiB = **95.6 GiB** | 同上 |
| Pro 5000 UUID | `GPU-f4334111-b8d2-4df3-ea0f-6177698f8ce9` | 同上 |
| Pro 6000 UUID | `GPU-d24ed424-5712-55e9-9b95-77d997ac80dc` | 同上 |
| MOSS ASR 稳态 | **7652 MiB = 7.5 GiB** @ `mem_fraction_static: 0.25` / 24 GiB 卡 | 2026-09-16 横评，3090 |
| MOSS ASR 权重 | 1.80 GB | SPIKE.md §4 |
| MOSS ASR 速度 | 460.2s 播客：3090 热态 **10.1s**；Pro 6000 **6.83s** | 2026-09-16 横评 |
| qwen3.8 权重 | **18.21 GiB**（safetensors 合计） | `ls` 实测 |
| qwen3.8 KV | **64 KiB/token**（64 层中仅 16 层 full attention） | config `layer_types` 统计 |
| WeMM-4B 稳态 | 17610 MiB = 17.6 GiB @ util 0.30 / 95.6 GiB | 2026-09-16 |
| WeMM-4B 地板 | 权重 8.6 + 峰值激活 12.4 = **21.0 GiB** | yaml 标定注释 |
| WeMM-4B KV 速率 | **23,090 tokens/GiB**（176,872 tok / 7.66 GiB） | 2026-09-15 vLLM 日志 |
| Unlimited-OCR 稳态 | 19353 MiB = 18.9 GiB @ util 0.22 / 95.6 GiB | 2026-09-16 |
| Unlimited-OCR 地板 | **未测** ← Task 3 要量的就是它 | — |
| Unlimited-OCR 精度 | CER **0.00%**（522 字全对，带版面框） | 2026-09-16 |

**推算（待 Task 3 校正）**：ASR 8.0 + qwen3.8 24.4 + WeMM-4B 23.8~26.7 = 56.2~59.1 GiB，剩 12.6~15.5 GiB。OCR 若地板 > 15 GiB 则四样无法共存。

## 待决问题（执行前需用户拍板）

1. **8 个 active 的 `image` 类服务往哪放？** `img-wikeeyang` / `img-flux2` / `wanwu-qianyi` / `ideogram4` / `studio-text-to-image` / `studio-image-edit` / `studio-upscale` / `studio-angle`。它们背后的原生 image 模型是 35.3~64.4 GiB，Pro 5000 放不下，只能与 ComfyUI 共用 Pro 6000（两者都非常驻，按需错峰）。若确认这些服务已废弃，可直接下线，Pro 6000 就真正独占。
2. **OCR 是否必须常驻？** 若 Task 3 量出装不下，选项是：① OCR 手动加载（调用前先 load）② embedding 降到 WeMM-2B ③ qwen3.8 上下文降到 16K 腾 KV。
3. **TTS 现状**：7 个 TTS 模型在注册表里但**零 active 服务**，本计划不为它们预留显存。若要用需另行规划。

---

### Task 1: 修好 MOSS ASR（当前是坏的，最高优先级）

横评时把 ASR 的 `gpu_uuid` 改成了 `GPU-78dcdbeb…`（已拔掉的 3090），`mem_fraction_static` 留在 0.25。该 UUID 现在指向不存在的卡，ASR 起不来。

**Files:**
- Modify: `backend/configs/models.d/moss_transcribe_diarize.yaml`（`gpu:` 第 24 行、`params.gpu_uuid:` 第 31 行、`vram_mb:` 第 14 行）
- Modify: `infra/moss-asr/moss_config.yaml`（`mem_fraction_static:` 第 32 行）

**Interfaces:**
- Produces：ASR 落 Pro 5000（GPU 0），稳态约 8 GiB，供 Task 4 的常驻容量核算使用。

- [ ] **Step 1: 改 yaml 钉到 Pro 5000**

`backend/configs/models.d/moss_transcribe_diarize.yaml`：
```yaml
vram_mb: 8500          # 原 15000。0.11 × 71.7 ≈ 7.9GiB 静态 + ~0.6 上下文
gpu: 0                 # 原 2（那张 3090 已拔）
```
`params` 块内：
```yaml
  gpu_uuid: GPU-f4334111-b8d2-4df3-ea0f-6177698f8ce9   # Pro 5000
```

- [ ] **Step 2: 改 mem_fraction_static 到 Pro 5000 口径**

`infra/moss-asr/moss_config.yaml` 第 32 行：
```yaml
      mem_fraction_static: 0.11
```
依据：3090 上 0.25 × 24 GiB = 6.0 GiB 静态、KV 33,428 tokens、实测总占 7.5 GiB。Pro 5000 上保持同样绝对值 → 6.0 / 71.7 = 0.084，取 **0.11** 留余量（0.11 × 71.7 = 7.9 GiB）。

- [ ] **Step 3: 跑静态测试**

```bash
cd backend && uv run pytest tests/test_model_scanner.py tests/test_resident_capacity.py -q
```
Expected: PASS（`test_resident_capacity` 会按新的 `vram_mb: 8500` 重算 GPU 0 常驻合计）

- [ ] **Step 4: 真机加载验证**

```bash
cd backend && TOKEN=$(grep -E "^ADMIN_TOKEN=" .env | cut -d= -f2- | tr -d '"')
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/engines/reload
curl -s -X POST -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/api/v1/engines/moss_transcribe_diarize/unload?force=true"
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/engines/moss_transcribe_diarize/load
```
Expected: `status` 最终为 `loaded`。sm_120 无 sm_86 的 JIT 问题，冷启动应在 1 分钟内。

- [ ] **Step 5: 确认真实落卡与占用**

```bash
for p in $(nvidia-smi --query-compute-apps=pid,gpu_uuid --format=csv,noheader | grep f4334111 | cut -d, -f1); do
  nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader | grep "^$p,"
  ps -o cmd= -p $p | cut -c1-70
done
```
Expected: 一个 `infra/moss-asr/.venv` 进程，占用 **7000–9000 MiB**。若显著偏离，回到 Step 2 调 `mem_fraction_static` 并重测。

- [ ] **Step 6: 功能验证（转写同一个播客）**

```bash
W=/media/heygo/program/projects-code/repos/nous-engine/infra/moss-asr/logs/podcast_16k.wav
P=<从 ss -tlnp 找 moss-asr venv 进程的监听端口>
for i in 1 2; do S=$(date +%s.%N)
  curl -s -X POST http://127.0.0.1:$P/v1/audio/transcriptions -F "file=@$W" -F "model=moss" \
    -F "response_format=verbose_json" -F "max_new_tokens=16384" -o /tmp/a.json
  E=$(date +%s.%N); echo "wall $(echo "$E-$S"|bc)s 段数 $(python3 -c "import json;print(len(json.load(open('/tmp/a.json'))['segments']))")"
done
```
Expected: 第 2 轮（热态）段数 53，wall 在 **6–11s** 之间（Pro 5000 介于 Pro 6000 的 6.83s 与 3090 的 10.1s 之间）。第 1 轮会含 JIT 预热，忽略。

- [ ] **Step 7: 把实测值回填 yaml 注释并提交**

把 Step 5/6 的真实占用与 wall time 写进 `moss_transcribe_diarize.yaml` 的 `vram_mb` 上方注释（替换掉「0.11 × 71.7 ≈ 7.9GiB」这句推算）。

```bash
git add backend/configs/models.d/moss_transcribe_diarize.yaml infra/moss-asr/moss_config.yaml
git commit -m "fix(asr): MOSS ASR 钉回存在的卡 —— 横评留下的 3090 UUID 已随卡拔除失效"
```

---

### Task 2: hardware.yaml 改成两卡布局

现在的 `groups` 还是三卡时代：`image`/`tts` 指着已不存在的 GPU 2，`llm-tp` 指着两张已拔掉的 3090。

**Files:**
- Modify: `backend/configs/hardware.yaml`（`groups:` 段及文件头的拓扑说明）

**Interfaces:**
- Consumes：Task 1 确定的 ASR 落卡（GPU 0）。
- Produces：`llm` group = `[0]`；删除 `llm-tp`；`image`/`tts` 重新指向。供 `GPUAllocator._build_groups` 与 `test_resident_capacity` 的容量推导使用。

- [ ] **Step 1: 改写 groups 段**

```yaml
groups:
  - id: llm
    gpus: [0]              # Pro 5000 72GB —— LLM/ASR/embedding/OCR 全在这张
    nvlink: false
    role: llm
    vram_gb: 72

  - id: image
    gpus: [1]              # Pro 6000 96GB —— 与 ComfyUI 共用(两者都非常驻,按需错峰)
    nvlink: false
    role: image
    vram_gb: 96

  - id: tts
    gpus: [0]              # Pro 5000;TTS 模型最大 7.8GiB,与推理服务共卡
    nvlink: false
    role: tts
    vram_gb: 72
```

**`llm-tp` 组整个删掉** —— 两张 3090 的 NVLink 对已不存在，且 Pro 5000 与 Pro 6000 异构不能组 TP（见 Global Constraints）。

- [ ] **Step 2: 同步更新文件头的三卡拓扑说明**

文件头现在写着「GPU 0: RTX 3090 (01:00) 24 GB → 显示卡」等，全部作废。改成：

```
# 当前 = Pro 6000 + Pro 5000 两卡布局（2026-09-20，两张 3090 已拔除）。
# 索引 = PCI_BUS_ID（与 nvidia-smi 一致）:
#   GPU 0: RTX PRO 5000 72GB Blackwell (01:00)  71.7 GiB 实测
#          → 全部推理服务:LLM / ASR / embedding / OCR
#   GPU 1: RTX PRO 6000 96GB Blackwell (99:00)  95.6 GiB 实测
#          → ComfyUI sidecar 独占（unit 用 UUID 钉卡，见 nous-engine-comfyui.service）
#
# **没有多卡 group** —— 两张卡型号不同，validate_gpu_group 对异构组直接报错
# （topology.py:269「GPU 组必须同型号」）。想做张量并行必须买同型号第二张。
#
# 显示输出走主板 ASPEED BMC（9c:00.0），不占用任何 N 卡。
```

- [ ] **Step 3: 验证组解析与容量测试**

```bash
cd backend && CUDA_DEVICE_ORDER=PCI_BUS_ID uv run python -c "
from src.config import load_hardware_config
for g in load_hardware_config()['groups']:
    print(g['id'], g['gpus'], g['vram_gb'])
"
uv run pytest tests/test_resident_capacity.py tests/test_gpu_allocator.py tests/test_gpu_group_tp.py -q
```
Expected: 打印 3 个 group（llm/image/tts），无 `llm-tp`；测试全绿。

- [ ] **Step 4: 提交**

```bash
git add backend/configs/hardware.yaml
git commit -m "config(hardware): 三卡改两卡 —— 删 llm-tp 组(3090 NVLink 对已拔)，llm 组移到 Pro 5000"
```

---

### Task 3: 实测三个 vLLM 模型在 Pro 5000 上的真实地板

**这是整个计划的数据前提。** 常驻名单取决于地板，而 OCR 的地板从未测过。本任务逐个单独加载、抓 vLLM 的显存标定日志，得到「权重 / 峰值激活 / KV / 最低可行 util」。

**Files:**
- Create: `backend/tests/manual/measure_pro5000_floors.py`
- Modify: 无（只测量，不改配置）

**Interfaces:**
- Consumes：Task 2 的 `llm` group = `[0]`。
- Produces：一张「模型 → (权重, 峰值激活, KV, 最低 util, 稳态占用)」表，Task 4 据此定常驻名单与各模型 util。

- [ ] **Step 1: 写测量脚本**

复用 `verify_wemm_embedding.py` 里 `_print_memory_profile` 的做法 —— vLLM 子进程 stdout 被适配器收在有界 deque（`_stdout_tail`）里不外打，必须捞出来。

Create `backend/tests/manual/measure_pro5000_floors.py`:
```python
"""量出各模型在 Pro 5000 上的真实显存地板(2026-09-20 两卡迁移 Task 3)。

地板 = 权重 + 峰值激活 + CUDAGraph(不含 KV)。它决定 util 的下限:低于它
vLLM 会报 "No available memory for the cache blocks" 直接起不来。
稳态占用则是它实际长期吃掉的量,决定几个模型能不能共存。

只加载不推理,逐个来(共存测试在 Task 4)。每个测完立即卸载。
"""
import asyncio, os, subprocess, sys, time
os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import yaml
from src.services.inference.llm_vllm import VLLMAdapter

DEVICE = "cuda:0"          # Pro 5000
CARD_GIB = 71.7

KEYS = ("KV cache", "model weights", "memory profiling", "GPU KV cache size",
        "Maximum concurrency", "non-torch memory", "peak activation", "Free memory on device")


def gpu_used_mib(uuid_frag="f4334111"):
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory,gpu_uuid",
                          "--format=csv,noheader"], capture_output=True, text=True).stdout
    return sum(int(l.split(",")[1].strip().split()[0]) for l in out.splitlines() if uuid_frag in l)


async def measure(model_id: str, util: float):
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "configs" / "models.d" / f"{model_id}.yaml").read_text())
    p = cfg.get("params", {})
    base = gpu_used_mib()
    ad = VLLMAdapter(
        paths=cfg["paths"], vllm_runner=p.get("vllm_runner"),
        max_model_len=p.get("max_model_len"), max_num_seqs=p.get("max_num_seqs"),
        gpu_memory_utilization=util, vllm_args=p.get("vllm_args"),
        quantization=p.get("quantization"),
    )
    print(f"\n{'='*70}\n### {model_id}  util={util}  ({util*CARD_GIB:.1f} GiB 预算)\n{'='*70}")
    t0 = time.perf_counter()
    try:
        await ad.load(device=DEVICE)
    except Exception as e:
        print(f"  ❌ 起不来: {type(e).__name__}: {str(e)[:200]}")
        for ln in list(getattr(ad, "_stdout_tail", []))[-25:]:
            if any(k.lower() in ln.lower() for k in ("memory", "cache blocks", "error")):
                print("   ", ln.strip())
        return
    print(f"  加载 {time.perf_counter()-t0:.1f}s")
    for ln in list(getattr(ad, "_stdout_tail", [])):
        if any(k.lower() in ln.lower() for k in KEYS):
            print("   ", ln.strip())
    time.sleep(3)
    print(f"  nvidia-smi 稳态增量: {gpu_used_mib()-base} MiB")
    ad.unload()
    time.sleep(5)


async def main():
    for mid, util in [(a.split("=")[0], float(a.split("=")[1])) for a in sys.argv[1:]]:
        await measure(mid, util)

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 确认 Pro 5000 是空的**

```bash
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv,noheader
```
Expected: GPU 0 的 `memory.used` < 200 MiB。若 ASR（Task 1）已在上面，先 `unload?force=true`，测完再加载回来。

- [ ] **Step 3: 测 OCR 的地板（从低 util 往上探）**

```bash
cd backend && set -a && source .env && set +a
uv run python tests/manual/measure_pro5000_floors.py unlimited_ocr=0.20
```
Expected: 要么成功并打印 `Available KV cache memory` / `Free memory on device` 那几行（从中读出权重与峰值激活），要么报 `No available memory for the cache blocks`（说明 0.20 低于地板）。

若失败，依次试 `0.24` → `0.28` → `0.32`，第一个成功的即为「地板 + 少量 KV」。**把每次的结果记下来。**

- [ ] **Step 4: 测 WeMM-4B 与 qwen3.8**

```bash
uv run python tests/manual/measure_pro5000_floors.py wemm_embedding_4b=0.34
uv run python tests/manual/measure_pro5000_floors.py qwen3_8_27b_abliterated_awq=0.34
```

⚠️ qwen3.8 的 yaml 此刻仍是 `gpus: [0,2]` + `tensor_parallel_size: 2`。本脚本绕过 ModelManager 直接构造 adapter 并钉 `cuda:0` 单卡，不受该字段影响 —— 但要确认 `vllm_args` 里没有 `--tensor-parallel-size`（有的话会被 `VLLM_ARGS_FORBIDDEN` 拒）。

Expected: 各自打印显存拆解。qwen3.8 权重应约 18.2 GiB。

- [ ] **Step 5: 汇总成表，判定常驻名单**

把四个模型（含 Task 1 已测的 ASR 7.5 GiB）填进下表：

| 模型 | 地板 | 最低可行 util | 选定 util | 预算 | 稳态 |
|---|---|---|---|---|---|
| MOSS ASR | — | — | 0.11(fraction) | 7.9 | 7.5（Task 1 实测） |
| qwen3.8 | ? | ? | ? | ? | ? |
| WeMM-4B | ? | ? | ? | ? | ? |
| Unlimited-OCR | ? | ? | ? | ? | ? |

判定规则：**所有常驻模型的预算之和 ≤ 67.7 GiB（71.7 − 4），且 vLLM 那几个的 util 之和 ≤ 0.92**（留 CUDA context）。装不下就按「待决问题 2」的三个选项取舍，**带着数字回去问用户，不要自行降配**。

- [ ] **Step 6: 提交测量脚本**

```bash
git add backend/tests/manual/measure_pro5000_floors.py
git commit -m "test(manual): 加 Pro 5000 显存地板测量脚本 —— 常驻名单要按实测定不能靠推算"
```

---

### Task 4: 按 Task 3 的实测结果改三个模型的落卡与 util

**Files:**
- Modify: `backend/configs/models.d/qwen3_8_27b_abliterated_awq.yaml`
- Modify: `backend/configs/models.d/wemm_embedding_4b.yaml`
- Modify: `backend/configs/models.d/unlimited_ocr.yaml`

**Interfaces:**
- Consumes：Task 3 Step 5 的实测表（各模型的选定 util 与预算）。
- Produces：三个模型都落 GPU 0，util 为 71.7 GiB 口径，常驻标记按判定结果。

- [ ] **Step 1: qwen3.8 改成 Pro 5000 单卡**

`qwen3_8_27b_abliterated_awq.yaml`：
```yaml
vram_mb: <Task 3 实测预算 × 1024>
gpu: 0                  # 原 gpus: [0, 2]，整行删掉换成这个
```
`params` 块内：
- **删掉 `tensor_parallel_size: 2`**（单卡不需要；留着会与 `_placement` 的结论冲突）
- `gpu_memory_utilization: <Task 3 选定值>`（原 0.70 是 24 GiB 卡口径，在 71.7 GiB 卡上会抓 50 GiB）

在 `vram_mb` 上方加注释，记清楚：原 TP=2 跨两张 3090 每卡 15.8 GiB，3090 拔除后改单卡；util 从 0.70 改成新值的原因是换卡口径变了。

- [ ] **Step 2: WeMM-4B 改 util 到 71.7 GiB 口径**

`wemm_embedding_4b.yaml`：
```yaml
vram_mb: <Task 3 实测预算 × 1024>
gpu: 0                  # 原 1
```
```yaml
  gpu_memory_utilization: <Task 3 选定值>    # 原 0.30 是 95.6GiB 口径 = 28.7GiB
```
保留 `resident: true`。第 13 行注释「0.30 × 95.6GiB ≈ 28.7GiB」要同步改成新卡的算式。

- [ ] **Step 3: OCR 改落卡与 util**

`unlimited_ocr.yaml`：
```yaml
vram_mb: <Task 3 实测预算 × 1024>
gpu: 0                  # 原 1
```
```yaml
  gpu_memory_utilization: <Task 3 选定值>    # 原 0.22 是 95.6GiB 口径 = 21.0GiB
```
`ttl_seconds: 3600` 保持（按需加载）。若 Task 3 判定它能常驻，才加 `resident: true` 并删掉 `ttl_seconds`。

- [ ] **Step 4: 静态测试**

```bash
cd backend && uv run pytest tests/test_model_scanner.py tests/test_resident_capacity.py tests/test_vllm_extra_args.py -q
```
Expected: PASS。`test_resident_capacity` 现在按 `hardware.yaml` 的 `llm` group（GPU 0，72 GiB）核常驻合计 ≤ 68 GiB。

- [ ] **Step 5: 提交**

```bash
git add backend/configs/models.d/
git commit -m "config(models): 三个 vLLM 模型迁到 Pro 5000 —— util 按 71.7GiB 重算，qwen3.8 退出 TP 改单卡"
```

---

### Task 5: 清理 DB runtime override

6 条 override 全部 `gpu: 1`，在旧布局里指 Pro 6000，现在必须指 GPU 0。**override 优先级高于 yaml，不清等于 Task 4 白做。**

**Files:**
- Modify: 数据库表 `model_runtime_overrides`（不是文件）

**Interfaces:**
- Consumes：Task 4 的 yaml 落卡结论。
- Produces：override 与 yaml 一致，不再互相打架。

- [ ] **Step 1: 记录当前状态（回滚锚点）**

```bash
cd backend && PGPASSWORD=$(grep -E "^DATABASE_URL=" .env | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|') \
  psql -h 127.0.0.1 -U nous -d nous_center -c \
  "SELECT model_id, resident, gpu, gpus, vram_budget_mode, vram_budget_value FROM model_runtime_overrides ORDER BY model_id;" \
  | tee /tmp/override_before_migration.txt
```

- [ ] **Step 2: 经 API 逐个改落卡（不要直接写 SQL）**

走 `PATCH /engines/{name}/gpu?gpu=0` —— 它会 reload registry + 失效缓存，直接改库不会。

```bash
TOKEN=$(grep -E "^ADMIN_TOKEN=" .env | cut -d= -f2- | tr -d '"')
for m in moss_transcribe_diarize qwen3_8_27b_abliterated_awq wemm_embedding_4b unlimited_ocr \
         wemm_embedding_2b qwen3_embedding_4b qwen3_embedding_8b; do
  echo "-- $m"
  curl -s -X PATCH -H "Authorization: Bearer $TOKEN" \
    "http://127.0.0.1:8000/api/v1/engines/$m/gpu?gpu=0"
  echo
done
```
Expected: 每条返回 `{"name":..., "gpu":0, "gpus":null, "applied":false, "hint":"需重新加载模型生效(unload + load)"}`

- [ ] **Step 3: 恢复 qwen3.8 常驻**

横评时为腾 3090 把它关了（`resident: f`）。

```bash
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/engines/qwen3_8_27b_abliterated_awq/resident?resident=true"
```

- [ ] **Step 4: 核对**

```bash
PGPASSWORD=... psql ... -c "SELECT model_id, resident, gpu, gpus FROM model_runtime_overrides ORDER BY model_id;"
```
Expected: 所有行 `gpu = 0`，`gpus = []`（显式清空组）；`qwen3_8_27b_abliterated_awq` 与 `moss_transcribe_diarize` 的 `resident = t`；WeMM-4B 无 resident 行（走 yaml 的 `resident: true`）。

---

### Task 6: ComfyUI 独占 Pro 6000（改成 UUID 钉卡）

unit 现在是 `CUDA_VISIBLE_DEVICES=1`。索引会随插拔漂移 —— 这次迁移就是因为索引变了才出一堆问题。改成 UUID 一劳永逸（MOSS ASR 早就这么做了）。

**Files:**
- Modify: `infra/systemd/nous-engine-comfyui.service:21`

**Interfaces:**
- Produces：ComfyUI 只见 Pro 6000，不会因索引漂移跑到 Pro 5000 上挤掉推理服务。

- [ ] **Step 1: 改 unit**

```
Environment=CUDA_VISIBLE_DEVICES=GPU-d24ed424-5712-55e9-9b95-77d997ac80dc
```
把原来的 `# PCI_BUS_ID 序实测(2026-08-10 本机三卡):0=3090, 1=Pro 6000(96G), 2=3090 → 绑 1` 注释换成：
```
# 用 UUID 钉卡而非索引 —— 2026-09-20 拔掉两张 3090 后索引从 1 变成 1(碰巧没变)，
# 但 2026-09-20 之前那次 Pro 5000 插在 01:00 时索引全部重排过。UUID 不随槽位/枚举变。
# GPU-d24ed424… = RTX PRO 6000 Blackwell 96GB。
```

- [ ] **Step 2: 安装并重启 sidecar**

```bash
sudo cp infra/systemd/nous-engine-comfyui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart nous-engine-comfyui
sleep 20 && systemctl is-active nous-engine-comfyui
```
Expected: `active`

- [ ] **Step 3: 确认 ComfyUI 只看得见 Pro 6000**

```bash
curl -s --max-time 8 http://127.0.0.1:8888/system_stats | python3 -c "
import json,sys; d=json.load(sys.stdin)
for dev in d.get('devices',[]): print(dev.get('name'), dev.get('vram_total'))"
```
Expected: 只有一条，且是 RTX PRO 6000，`vram_total` ≈ 101973491712（95 GiB）。**不能出现 Pro 5000。**

- [ ] **Step 4: 提交**

```bash
git add infra/systemd/nous-engine-comfyui.service
git commit -m "config(comfyui): 改用 UUID 钉 Pro 6000 —— 索引会随插拔漂移"
```

---

### Task 7: 端到端验证

**Files:** 无（只验证）

- [ ] **Step 1: 重启后端，让常驻按新配置预加载**

```bash
sudo systemctl restart nous-engine-backend && sleep 180
```

- [ ] **Step 2: 确认落卡与状态**

```bash
cd backend && TOKEN=$(grep -E "^ADMIN_TOKEN=" .env | cut -d= -f2- | tr -d '"')
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/engines | python3 -c "
import json,sys
rows=json.load(sys.stdin); rows=rows if isinstance(rows,list) else rows.get('engines',[])
for e in rows:
    if e.get('status') in ('loaded','loading','failed') or e.get('resident'):
        print(f\"{e['name']:<34}{str(e.get('status')):<10}resident={e.get('resident')} gpu={e.get('loaded_gpu')}\")"
nvidia-smi --query-gpu=index,name,memory.used,memory.free --format=csv
```
Expected: 常驻模型全部 `loaded` 且 `gpu=0`；**GPU 1（Pro 6000）上只有 ComfyUI**；GPU 0 剩余显存 > 5 GiB。

- [ ] **Step 3: 数据面实调（铸临时 key，验完删）**

```bash
# 建一把授权给 moss-asr / wemm-embedding-4b 的临时 key，然后:
#   POST /v1/audio/transcriptions   ← ASR
#   POST /v1/embeddings (messages)  ← embedding，必须走 messages 不能传 input
#   POST /v1/chat/completions       ← qwen3.8
```
Expected: 三个都 200。embedding 返回 2560 维。

- [ ] **Step 4: 复跑三路横评，与旧数据对比**

```bash
cd backend && set -a && source .env && set +a
uv run python tests/manual/bench_asr_ocr_embed.py --only asr,embed
```
Expected 对照（Pro 6000 基线 → Pro 5000）：

| 项 | Pro 6000 | 3090 | Pro 5000 预期 |
|---|---|---|---|
| ASR 460.2s 播客 | 6.83s | 10.1s | 7–9s |
| embedding 文本 | 237 ms/条 | — | 250–320 ms/条 |
| embedding 图像 | 439 ms/张 | — | 460–600 ms/张 |

慢于预期区间要查是不是 util 给太小导致 KV 不足。

- [ ] **Step 5: 全量测试 + 提交**

```bash
uv run pytest tests -n 8 -q --ignore=tests/integration
git add -A && git commit -m "docs: 两卡布局迁移完成 —— 实测数据回填"
```

---

### Task 8: 更新 CLAUDE.md 的 GPU 章节

CLAUDE.md 的「GPU 放置 / 张量并行」整节都是三卡布局的描述，迁移后全部作废。这节是每次会话都加载的，留着错的比没有更糟。

**Files:**
- Modify: `CLAUDE.md`（「## GPU 放置 / 张量并行 (GPU groups)」一节）

- [ ] **Step 1: 改写该节的硬件描述**

把「本机三张卡(PCI 序):`cuda:0` = RTX 3090 24G(**驱动显示器**)、`cuda:1` = RTX PRO 6000 96G、`cuda:2` = RTX 3090 24G。0 与 2 之间有 NVLink」整段换成两卡描述，并明确：

- 没有任何多卡 group，异构卡不能做 TP
- 显示输出走主板 ASPEED BMC，不占 N 卡
- GPU 0 = Pro 5000（全部推理服务），GPU 1 = Pro 6000（ComfyUI 独占）
- **GTX 1060 等 Pascal 卡在本机不可用**：驱动 595 是开源内核模块，要求 GPU 带 GSP（Turing 及以后才有），Pascal probe 直接失败；且 Blackwell 必须 ≥595 而 Pascal 需 ≤580，一台机器一个驱动版本，互斥无解。

- [ ] **Step 2: 保留仍然成立的不变式**

以下几条与卡数无关，**不要删**：放置决策只在 `_resolve_placement` 一处、显式 gpu/gpus 是硬约束、`topology.resolve_gpus` 是唯一实现、`group_budget_gb` 取组内最小、绝不存在「不钉卡」的启动分支、数据面对放置只读。

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs(claude): GPU 章节改两卡布局 —— 三卡描述与 llm-tp 组已随 3090 拔除作废"
```

---

## 回滚

任一 Task 出问题：

1. **配置**：`git checkout -- backend/configs/ infra/`
2. **DB override**：按 `/tmp/override_before_migration.txt`（Task 5 Step 1 存的）用 `PATCH /engines/{name}/gpu?gpu=N` 逐条改回
3. **服务**：`sudo systemctl restart nous-engine-backend nous-engine-comfyui`

注意 Task 1 之前的 ASR 配置是**坏的**（指向已拔除的 3090），回滚到那个状态没意义 —— ASR 的修复应当保留。
