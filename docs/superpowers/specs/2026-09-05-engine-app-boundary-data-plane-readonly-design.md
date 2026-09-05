# nous-engine 与下游的边界:数据面对模型放置只读

**Status**: Draft
**Author**: heygo(设计 by Claude)
**Date**: 2026-09-05

## 0. 一句话

nous-engine 对下游(nous-app、ComfyUI、任何持 InstanceApiKey 的调用方)是**模型提供商**,
角色等同于火山/阿里对 nous-app。提供商要么有容量、立刻服务,要么立刻拒绝;**一个请求
绝不能改变提供商显存里有什么**。今天(2026-09-05)整整一天的显卡争抢,根因就是这条
边界不存在。

## 1. 事故证据(为什么现在做)

本节是设计的依据,每条都在真机上核实过,不是推断。

1. **数据面能唤醒模型**。`vllm_endpoint.py::ensure_vllm_base_url` 在 `/v1/chat/completions`
   等路径上按需 `load_model`。nous-app 的 worker(容器 `nous-worker`)调一次
   `qwen3-6-35b`,请求**阻塞 85–111 秒**(冷加载),然后 Qwen3.6 落到 `[0,2]` 那对 3090
   ——正是 Qwen3.8 显式钉死的组。**更正(2026-09-05 复审)**:它落这组不是「自动选组」,
   而是 `model_runtime_overrides` 里给 `qwen3_6_35b_fp8` 留着一行 `gpus=[0,2]`,
   即显式硬约束。所以这条事故的成因是**覆盖表里的历史落卡 + 数据面能唤醒它**两者相乘,
   不是自动选卡选错了组。
2. **一旦被唤醒就永久占卡**。`startup_reconcile.py:44` 给每个已发布工作流的模型依赖
   登记进程级引用(`add_reference(dep["key"], str(wf.id))`),`check_idle_models`
   (`model_manager.py:1053`)见引用非空即 `continue`。一个叫「新工作流」的 3 节点
   玩具图(text_input → llm(qwen3_6) → text_output,2026-04-30 建)引用了 3.6,于是
   3.6 加载后**永不 TTL 回收**。而 `resident: false` 写在 yaml 里,`/api/v1/engines`
   上看不出任何异常。
3. **`unload` 撒谎**。`engines.py::unload_engine` 调 `unload_model(name, force)` 后
   **丢弃返回值**,无条件返回 `{"status":"unloaded"}`。`unload_model` 在有引用时
   `return False` 且只打 `logger.debug`。`resident` 有 409 守卫提示 `force=true`,
   **引用没有对应守卫**。实测:返回 200、模型仍在 `loaded` 列表、vLLM 进程活着、
   20.5G 显存不退。
4. **慢失败会干扰下游**。nous-app 的模型目录里火山/deepseek 都绿,只有指向 nous-engine
   的两行红——这本身是正确的(提供商挂了就是一行红)。但一次 85 秒的阻塞跟一次瞬时
   503 对编排链路的影响完全不同:前者吃掉整条链的超时预算。
5. **容量账目失真**(顺带,已修 #710):MOSS 搬去 Pro 6000 后 `mem_fraction_static: 0.5`
   从 12G 变 48G,而 yaml 仍写 `vram_mb: 13000`,分配器对 Pro 6000 的账目差 36G。

## 2. 决策记录(四问四答,已与用户确认)

| 问题 | 决定 |
|---|---|
| 下游请求未加载的模型,怎么回应 | **直接拒绝并暴露状态**。谁常驻、何时加载完全由 engine 决定 |
| 「不得触发加载」按什么划线 | **推理端点一律不加载**(含 admin bearer)。加载只经控制面 `/api/v1/engines/{name}/load` |
| 状态怎么暴露 | **`/v1/models` 只列就绪的**。nous-app 现成的"列模型"探活不改一行就能反映真实状态 |
| 目录承诺哪些模型常驻 | **单 LLM:Qwen3.8 取代 3.6**。3090 对常驻 3.8,Pro 6000 放 MOSS + embedding |

附带一条身份决定:nous-engine 对外以自己的名字出现(`owned_by: "nous-engine"`),
不借用 `openai` / `nous-center`。

## 3. 目标 / 非目标

**目标**
1. 数据面对模型放置只读:任何 `/v1/*` 请求都不能改变显存里有什么。
2. 快失败:未就绪即刻拒绝(微秒级、无 I/O),不再存在阻塞式冷启动。
3. `/v1/models` = 现在就能调的。
4. 常驻是显式配置(`resident: true`),不存在任何隐式常驻。
5. `unload` 说真话:拒绝就返回拒绝和原因。
6. 「为什么这个模型还在显存里」永远能从一个端点看出来。

**非目标**
- 不改 nous-app(它的目录、探活、provider 命名都是它自己的事)。
- 不做按 key 的唤醒权限、不做 ServiceInstance 级的「允许唤醒」开关。
- 不做「冷启动中 + Retry-After」的半状态。
- 不改 GPU 放置算法、不做多进程、不做集群。
- 不在本 spec 内决定 3.8 要不要补工具调用参数(目录能力,单开)。

## 4. 边界与不变式

**控制面** = `/api/v1/engines/*`(admin 鉴权)+ 进程内的开机预热 / 服务自启 / 看门狗自愈。
这是**唯一**能改变放置(load / unload / place)的地方。

**数据面** = `/v1/*` 全部兼容路由:`openai_compat`(chat / embeddings / audio 等)、
`anthropic_compat`、`ollama_compat`、`responses`、`context_cache`。数据面只做两件事:
解析**已加载**模型的 HTTP 端点,然后代理。

**关门只有一扇**。全仓库调 `load_model` 的地方共五处:`vllm_watchdog.py:61`(自愈)、
`main.py:688`(开机常驻预热)、`service_autostart.py:132`(服务自启)、`engines.py:395`
(显式 `/load`)、`vllm_endpoint.py:82`(`ensure_vllm_base_url`)。前四处都是控制面;
**第五处是数据面唯一的门**。画布工作流执行器(`llm_runner.py`)本来就用只读的
`get_vllm_base_url`,`_load_wf_deps` 已于 2026-09-03 删除。

**这条「只有一扇门」限定在上述五个 LLM 兼容模块**(2026-09-05 复审补正)。画布工作流的
`POST /v1/services/{name}/predictions` 经 `nodes/llm.py`、图像路径经
`get_or_load_image_adapter`,仍会在执行期按需加载模型——它们不在本不变式的射程内
(见 §6 的范围界定),要不要一并收紧另开 spec。

**落地**
- 删除 `ensure_vllm_base_url`。六处调用(`openai_compat.py` ×3、`anthropic_compat.py`、
  `ollama_compat.py`、`context_cache.py`、`responses.py`)改为 `get_vllm_base_url`。
- 新增结构性守卫测试:静态断言 `src/api/routes/` 下的数据面模块(上列五个文件)
  不引用 `load_model`、`ensure_vllm_base_url` 或任何加载能力。与 `_placement.py`
  的三条不变式同一路数——把「别踩」变成「踩不到」。
- CLAUDE.md 新增一条:**模型放置只能由控制面改变;数据面对放置只读**。

## 5. 错误契约(快失败)

模型已授权给该 key、但当前未加载(**含 `loading` 中**)→ **HTTP 503**,OpenAI 风格错误体:

```json
{
  "error": {
    "type": "model_not_ready",
    "code": "model_not_ready",
    "param": "model",
    "message": "model 'qwen3-8-27b' is not loaded; see GET /v1/models for ready models",
    "ready_models": ["moss-asr"]
  }
}
```

- `ready_models` = 该 key 已授权 **∩** 当前 `is_loaded` 的服务名,与 §6 的 `/v1/models`
  用同一个 helper 算,保证两处口径一致。
- 判定只读内存中的 `is_loaded`,**不取 `_lock_for` 加载锁、不做任何 I/O**。控制面正在
  加载是控制面的事,数据面不等。
- 流式请求在**开流前**拒绝,客户端拿到的是普通 503 而非断开的 SSE。
- 未授权 / 不存在 → 保持现有 404 `model_not_found`。「没就绪」与「没授权」是两个码。
- 现有 `VLLMNotLoaded → 503` 的映射保留,只是不再有人在它前面先阻塞几十秒。

## 6. `/v1/models` 就绪语义与身份

- `source_type == "model"` 的服务,只在其后端模型 `is_loaded` 时列出。
- `comfy_template` / `workflow` / `app` 等不占 nous-engine 显存的服务**照旧按授权列**
  ——它们的就绪由各自执行路径负责,本 spec 不动。
- `GET /v1/models/{id}`:已授权但未加载 → 与 §5 相同的 503 `model_not_ready`;
  未授权 → 404。
- `ModelObject.owned_by` 从 `"nous-center"` 改为 `"nous-engine"`。
- `?type=` 过滤保留。**不加** `?include=all`(用户明确选了只列就绪;YAGNI)。

## 7. 常驻显式化——废除工作流引用的隐式常驻

本 spec 唯一有波及面的改动,也是事故证据 #2 的直接修复。

**改法**
- 删除 `startup_reconcile.py` 对已发布工作流依赖的 `add_reference` 登记。
- 删除 `workflows.py`(下架)与 `services.py`(删服务)里配套的 `remove_reference` +
  顺带 `unload_model`。工作流的生命周期不再影响模型驻留。
- **保留** `openai_compat.py:352` 的 `proxy_ref`:请求期短寿引用,防流式输出中途被
  memory_guard / TTL 驱逐,是引用机制的正确用法。执行期的硬保护本来就是 `_in_use`
  (卸载正在推理的 adapter 会 segfault),不依赖长期引用。
- 结论:`resident: true` 成为**唯一**的钉住手段。yaml 写什么,行为就是什么。

**为什么不是"保留引用但让它不挡 TTL"**:那会留下第三种状态(挡 LRU 不挡 TTL),继续
给「为什么它还在显存里」添解释成本。引用机制只保留一个语义:正在服务的请求。

## 8. `unload` 诚实化

`engines.py::unload_engine`:
- 接住 `unload_model` 返回值。`False` → **409**,`detail` 结构化说明原因:
  `{"reason": "in_use" | "resident" | "referenced", "referenced_by": [...], "hint": "..."}`。
- `resident` / `referenced` 的 hint 提示 `force=true`;`in_use` 的 hint 明确说明
  **force 也不覆盖**(卸载正在推理的 adapter 会 segfault),请等请求结束。
- `unload_model` 拒绝时的日志从 `logger.debug` 提到 `logger.info`。
- `force=true` 语义不变:绕过 `resident` 与引用,不绕过 `in_use`。

## 9. 可观测

`/api/v1/engines` 每项新增:
- `held_by`: 当前活跃引用列表(§7 落地后正常为空,或只剩短寿的 `proxy-*`)。
- `resident`(已有)。

目的:「为什么这个模型还在显存里」的答案永远在这一个端点上——要么 `resident`,
要么 `held_by` 非空,没有第三种。

## 10. 容量落地(目录)

| 模型 | 落卡 | resident | 备注 |
|---|---|---|---|
| `qwen3_8_27b_abliterated_awq` | `gpus: [0, 2]`(已钉) | **true** | 目录唯一 LLM。tp=2,每卡 ~16.5G |
| `qwen3_6_35b_a3b_fp8` | — | — | **删除 yaml**,权重留盘。从目录退役 |
| `qwen3_embedding_8b` | `gpu: 1` | **true** | 17.5G。此前无钉卡、自动选卡,反复被 LRU 驱逐 |
| `moss_transcribe_diarize` | `gpu: 1`(已钉) | true(已有) | #710 后稳态 17.4G |

Pro 6000 账目:MOSS 17.4 + embedding 17.5 + ComfyUI 峰值 ~22 ≈ 57G / 96G,从容。
3090 对:3.8 独占,不再有人抢。

**新增配置测试**:所有 `resident: true` 的模型按落卡汇总 `vram_mb`,必须放得进
`hardware.yaml` 容量减 `DEFAULT_RESERVED_GB`,否则测试红。常驻集合不自洽在合 PR 前
暴露,不等上线。

## 11. 测试

全部跑在 PostgreSQL、mock `subprocess.Popen`(CLAUDE.md 铁律,不碰 GPU)。

1. **结构守卫**:数据面五个模块无加载能力引用(静态扫描)。
2. **快失败**:chat / embeddings / responses / anthropic / ollama / context_cache 对已授权
   未加载模型 → 503 `model_not_ready`,`ready_models` 正确;断言 mock manager 的
   `load_model` **未被调用**;流式请求同样在开流前 503。
3. **`/v1/models`**:按 `is_loaded` 过滤;非模型服务不受影响;`owned_by == "nous-engine"`;
   `GET /v1/models/{id}` 未加载 → 503、未授权 → 404。
4. **`unload` 诚实**:manager 返回 `False` → 409 含 `reason`;`force=true` 对 `resident`
   与 `referenced` 生效、对 `in_use` 不生效。
5. **引用**:已发布工作流不再登记长期引用;非常驻模型即使被已发布工作流引用也会被
   idle TTL 回收;`proxy_ref` 在请求期间仍阻止回收、请求结束后释放。
6. **可观测**:`/api/v1/engines` 的 `held_by` 反映活跃引用。
7. **容量**:常驻集合容量测试(§10)。

## 12. 验收(真机联调,非 CI)

spec 的意义在于下游真的能按提供商语义用它,所以验收在真机、用真下游。

1. **就绪即服务**:3.8 常驻后,ComfyUI 的 `ImageCaptionNode`(反推)与 `PromptExpand`
   (扩写)经 InstanceApiKey 调用成功;nous-app 探活列到 `qwen3-8-27b`。
2. **未就绪即拒绝、且不产生副作用**:经控制面 `unload?force=true` 卸掉 3.8 后,同样的
   调用在 **<100ms** 内返回 503 `model_not_ready`;`nvidia-smi` 显示 GPU 0/2 显存
   **不变**——请求没有唤醒任何东西。再经控制面 `load` 恢复。
3. **ComfyUI 人像工作流整条跑通**(用户硬性要求):
   - 总控 `Krea2-全能总控-11模式.json` 的模式 12(Z-Image 双阶段)+ 反推开关:
     **已于 2026-09-05 实跑出图两张**(`Krea2_00022_.png` / `Krea2_00023_.png`,
     1920×1088),反推文本正确驱动了侧脸 + 暖色调的改动。
   - 独立的 `Aiden-极致真实摄影人像工作流.json`:反推开关置 true → Z-Image 双阶段 →
     flux-2-klein-9b 精修 → SeedVR2 7B 放大 → SaveImage,**整条不 bypass**,落地一张
     图。以 SaveImage 产物路径 + 分辨率 + 反推文本为证。
4. **下游只反映状态**:nous-app `mediahub_models` 里 `nous-qwen3-llm` 行的红绿
   纯粹随 engine 的加载状态变化,且 nous-app 的火山/deepseek 行不受任何影响
   (观察项,不是 nous-engine 的义务)。

## 13. 范围外与后续

- nous-app 侧:`mediahub_models` 的 `actual_provider` 叫 `openai`、`actual_model`
  指向已退役的 `qwen3-6-35b`——它那边的命名与目录,由它自己改或不改。
- 「新工作流」(`308084173191516160`)与挂在它上面的 active 服务 `ltx-drama`:§7 落地
  后它不再有副作用,处置是运维决定。
- 3.8 是否补 `--enable-auto-tool-choice --tool-call-parser`(3.6 有、3.8 没有):
  目录能力问题,经 `params.vllm_args` 透传即可,单开。
- `asr_sglang.py:92-93` 的 3090 UUID 默认回退与「绝不 Pro 6000」过时注释:死代码,单开。
- **覆盖表可以无声取消 yaml 的 `resident`**:`model_runtime_overrides` 里一行
  `resident=False` 就能让目录里的 `resident: true` 完全失效,且此前零日志(真机上
  `qwen3_embedding_8b` 正是如此)。已在 `registry.py` 加不一致告警(resident → error、
  落卡 → warning,本 PR)。**告警不是修复**:两处真相源(yaml 目录 / DB 覆盖)谁是权威
  仍未收敛,要收敛得单开。
- **画布工作流的 prediction 仍可唤醒模型**:`nodes/llm.py` / `get_or_load_image_adapter`
  在执行期按需加载,§4 的「数据面只读」不覆盖这条路径。要把同一条不变式推到工作流执行器,
  单开 spec(要先回答「工作流执行到底算控制面还是数据面」)。
- CLAUDE.md 里「`/v1/*` 校验失败会退回 admin-session」的说法对
  `/v1/audio/transcriptions` 不成立(`_auth_transcriptions` 直连
  `verify_bearer_token_any`),需修正措辞。

## 14. 实施顺序(供 writing-plans 参考)

1. §8 `unload` 诚实化 + §9 `held_by`——最小、独立、立刻让今天的现象可见。
2. §7 废除工作流长期引用——修根因。
3. §4 关门 + §5 错误契约 + §6 `/v1/models`——一起做,共用 `ready_models` helper。
4. §10 目录配置 + 容量测试——最后,此时 3.6 退役、3.8/embedding 常驻。
5. §12 真机验收。

## 15. 回滚

每步独立 PR。§4 的回滚是恢复 `ensure_vllm_base_url` 与六处调用(纯代码);§7 的回滚
是恢复 `add_reference` 登记;§10 的回滚是恢复 yaml。没有数据迁移,不涉及 Alembic。
