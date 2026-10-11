# Strata 外部服务接入

nous-engine 只连接已经在本机运行并完成模型加载的 Strata 服务，不负责安装、启动、停止或卸载 Strata。
数据面请求也不会触发 Strata 自动加载模型。

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
