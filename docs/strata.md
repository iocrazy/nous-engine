# Strata 接入

外部模式下，nous-engine 只连接已经在本机运行并完成模型加载的 Strata 服务，不负责安装、启动、停止或卸载 Strata。
数据面请求也不会触发 Strata 自动加载模型。

这是默认的外部模式。需要由 nous-engine 管理生命周期时，使用下方的 systemd 托管模式；
Strata 仍运行在独立 cgroup 中，避免计入后端的 `MemoryMax`。

## 前置条件

- Strata 监听 `127.0.0.1` 或其他 loopback 地址，使用 HTTP。
- Strata 未启用 API key。当前 nous-engine 的直连代理不会向上游转发认证头。
- `GET /health` 返回 `service: "strata"`、`loaded: true` 和有效的 `max_context`。
- 为模型显式配置实际使用的 `gpu`。外部服务无法由 ModelManager 可靠推断落卡。

## 模型配置

在 `backend/configs/models.d/` 增加一个模型文件，例如：

```yaml
adapter: src.services.inference.llm_strata.StrataAdapter
id: qwen3_8_flash_next_strata
source: Qwen/Qwen3.8-Flash-Next
paths: {}
resident: false
type: llm
vram_mb: 0
gpu: 0
params:
  strata_base_url: http://127.0.0.1:8080
```

`resident` 应保持 `false`，除非 Strata 保证在 nous-engine 启动前已经运行且模型已经加载。
`vram_mb` 在这一阶段不参与 Strata 的动态 VRAM/RAM 规划；资源隔离仍由 Strata 侧配置负责。

完成模型配置后，使用控制面加载该引擎。加载操作只执行健康检查并建立连接；nous-engine 卸载引擎时只断开连接，不会调用 Strata 的 `/v1/unload` 或终止其进程。

要对外提供服务，还需像其他模型一样创建以该引擎为 source 的 `model` 服务实例。Chat Completions、Responses、Anthropic 与 Ollama 兼容路由都会复用 Adapter 暴露的本机 `base_url`。

## 当前边界

- 只支持本机外部服务，不接受远程地址、HTTPS、URL 凭据或带路径的 base URL。
- 不管理 Strata 的安装、模型下载、进程生命周期、RAM 预算和显存弹性配置。
- Strata 默认串行处理请求；并发行为由 Strata 自己的 `parallel` 配置决定。
- nous-engine 的 `thinking` 扩展仍按 vLLM 语义处理。直接调用时应使用 Strata 原生的 `reasoning_effort` 字段。

## systemd 托管模式

托管模式使用 `ManagedStrataAdapter`。适配器不直接以子进程运行 Strata，而是通过
root 属主的 `/usr/local/lib/nous-engine/strata-control` 启停固定格式的
`nous-engine-strata@<instance>.service`。helper 只接受 `start|stop` 和经过严格校验的实例名。

Strata 安装、pack 和 MTP 准备仍是一次性 provisioning，不在模型加载请求中执行。
推荐目录：

- 原始 GGUF：`/media/heygo/program/models/nous/llm/<model>/`
- pack、MTP：`/media/heygo/program/models/nous/runtime/strata-data/`
- Strata 配置：`/media/heygo/program/projects-code/github-repos/Strata/strata-<model>.json`
- Strata checkout 和 venv：`/media/heygo/program/projects-code/github-repos/Strata/`

每个实例需要 root 创建 `/etc/nous-engine/strata/<instance>.env`，例如：

```text
STRATA_CONFIG=/media/heygo/program/projects-code/github-repos/Strata/strata-iq3_s.json
STRATA_PORT=18080
```

模型配置示例：

```yaml
adapter: src.services.inference.llm_strata.ManagedStrataAdapter
id: qwen3_8_flash_next_iq3s_strata
source: Qwen/Qwen3.8-Flash-Next
type: llm
paths:
  main: llm/qwen3.8-flash-next/IQ3_S
resident: true
vram_mb: <measured-peak-mib>
gpu: <dedicated-gpu-index>
params:
  strata_base_url: http://127.0.0.1:18080
  strata_instance: qwen38-iq3s
  max_model_len: 8192
  strata_context_presets:
    8192: qwen38-iq3s
    262144: qwen38-iq3s-256k
    524288: qwen38-iq3s-512k
  vision: true
```

配置 `strata_context_presets` 后，引擎页只显示已配置的上下文档位。模型已加载时，选择档位会
停止当前实例并后台启动目标实例；模型未加载时只保存选择。正在推理的模型不会被切换。
适配器会核对 `/health.max_context`，避免实例环境文件指错配置后以错误档位提供服务。

`resident: true` 让后端异常重启后重新连接仍在运行的专用 unit；正常关闭时
`ModelManager` 会依次卸载模型并停止 unit。`gpu`、`vram_mb` 和 Strata 配置里的设备必须一致。
占用生产 ComfyUI 所在 GPU 前必须另行确认维护窗口，不能把示例占位符直接用于部署。

模板 unit 默认 `MemoryHigh=68G`、`MemoryMax=72G`，且不会被安装脚本自动启用或启动。
实际启用前必须根据目标模型的峰值 RAM 调整并完成真机验收。视觉配置需要同时包含 mmproj、
server 的 `vision` 段，以及引擎参数中的 `--vision`。

## 本机 IQ3_S 实例

`qwen3_8_flash_next_abliterated_iq3s_strata` 已按 GPU 1 的按需模型登记，配置为 8K 上下文、
CPU 视觉编码器和 90,000 MiB 显存预算。它不会随后端启动自动加载。

部署前由维护人员创建三个环境文件，全部复用 18080 端口，因为同一模型任一时刻只运行一个实例：

```text
# /etc/nous-engine/strata/qwen38-iq3s.env
STRATA_CONFIG=/media/heygo/program/projects-code/repos/nous-engine/infra/strata/qwen38-iq3s-8k.json
STRATA_PORT=18080

# /etc/nous-engine/strata/qwen38-iq3s-256k.env
STRATA_CONFIG=/media/heygo/program/projects-code/repos/nous-engine/infra/strata/qwen38-iq3s-256k.json
STRATA_PORT=18080

# /etc/nous-engine/strata/qwen38-iq3s-512k.env
STRATA_CONFIG=/media/heygo/program/projects-code/repos/nous-engine/infra/strata/qwen38-iq3s-512k.json
STRATA_PORT=18080
```

256K 使用模型原生上下文和 INT8 KV；512K 使用 YaRN 2 倍扩展，属于实验档位，需分别做
长提示词质量、峰值显存和峰值 RAM 验收后再开放给长期任务。

生成的 Strata 配置必须保持 `gpu: 1`，且 `--vram-reserve-mib` 不得低于 5000。
GPU 1 同时承载生产 ComfyUI；加载该模型只能在明确维护窗口内进行，并确保没有 ComfyUI 工作流运行。

## 本机 Uncensored IQ4_XS 实例

`qwen3_8_flash_next_uncensored_iq4xs_strata` 是独立于 IQ3_S 的按需模型，使用
`Uncensored-IQ4_XS/Qwen3.8-Flash-Next-Uncensored.IQ4_XS.gguf`、`orca-iq4_xs`
pack 和该模型自己的 mmproj。它与 IQ3_S 共用 GPU 1 和 18080 端口，不能同时加载。

部署前由维护人员创建三个环境文件：

```text
# /etc/nous-engine/strata/qwen38-iq4xs.env
STRATA_CONFIG=/media/heygo/program/projects-code/repos/nous-engine/infra/strata/qwen38-iq4xs-8k.json
STRATA_PORT=18080

# /etc/nous-engine/strata/qwen38-iq4xs-256k.env
STRATA_CONFIG=/media/heygo/program/projects-code/repos/nous-engine/infra/strata/qwen38-iq4xs-256k.json
STRATA_PORT=18080

# /etc/nous-engine/strata/qwen38-iq4xs-512k.env
STRATA_CONFIG=/media/heygo/program/projects-code/repos/nous-engine/infra/strata/qwen38-iq4xs-512k.json
STRATA_PORT=18080
```

8K 用于首次真机验收；256K 使用原生上下文与 INT8 KV；512K 使用 YaRN 2 倍扩展，
在长提示词质量、峰值显存和 RAM 验收通过前保持实验用途。
