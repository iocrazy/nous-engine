# Skill 驱动工作流编排 API(skill-runs)设计

- 日期:2026-09-26
- 状态:已评审(brainstorming 三节逐节确认)
- 首批接入:Qwen Image 2.1 文生图(`qwen21-text-to-image`)与图像编辑(`qwen21-image-edit`)

## 1. 目标与边界

提供两个**通用**端点,把「用 Chat 模型执行一段 Skill → 得到一段文本 → 注入任意已发布
工作流的某个文本字段 → 提交」标准化:

- `POST /v1/skill-runs/preview`:用指定 Chat 模型执行 Skill,返回最终文本。
- `POST /v1/skill-runs/generate`:把最终文本写入调用方指定的 `prompt_field`,提交任意
  已发布工作流服务(`workflow` / `comfy_template`)。

硬约束:

1. 编排代码**不得**依赖 Qwen Image、ComfyUI 节点 ID 或任何特定服务名。
2. Qwen Image 2.1 的节点映射与参数属于**模板管理数据**(`service_instances.exposed_inputs`,
   经 `/api/v1/comfy-templates/{id}/mapping` 维护),不进编排代码。
3. 未来视频 / 音频 / 3D 等带文本输入的工作流**零代码**接入:调用方选它的 string 字段作
   `prompt_field` 即可。

适用边界:**Skill 最终输出一段文本,注入工作流的某一个字段**。Skill 同时产出 prompt /
negative prompt / 比例 / 镜头参数等结构化字段的「结构化输出映射」**不在本期**,需要时
另开 spec;本期不预埋任何结构化字段。

明确不做:结构化输出映射、流式 preview、preview 结果落库、引擎侧 Skill 存储、前端 UI。

## 2. 关键决策

| 决策 | 选择 | 理由 |
|---|---|---|
| Skill 来源 | 调用方**内联**传 SKILL.md 全文 | Skill 归上层平台(nous-app)管;引擎对 Skill 无状态。现有 `~/.nous-center/skills` 文件库不参与 |
| `/generate` 入参 | **只收最终文本** | 两步解耦:调用方可展示/让用户改 prompt;出图失败重试不重跑 LLM。多一次内网往返 + 几 KB 文本 ≈ 毫秒级,相对 LLM 秒级 + 出图数十秒可忽略 |
| preview 输入 | 文本 + 可选图片(OpenAI content parts) | qwen3.8-27B 是视觉语言模型;编辑类 Skill 需要看原图才能写出有针对性的编辑指令 |
| 实现方式 | **抽公共服务函数**(方案 A) | chat 与 prediction 的 auth / grant / quota / readiness / 计量各保持唯一实现,避免「两个合并点漏一个」类事故。弃用:内部 HTTP 转调(B)、另写精简 chat(C) |

## 3. API 契约

### 3.1 `POST /v1/skill-runs/preview`

请求:

```jsonc
{
  "model": "qwen3-8-27b",                     // model 类 Chat 服务名(ServiceInstance.name)
  "skill": { "content": "---\nname: x\n---\n正文…" },  // SKILL.md 全文;对象形给以后扩展留位
  "input": "黄昏窗台上的猫",                    // 字符串,或 content parts 数组(见下)
  "options": {                                 // 全部可选
    "temperature": 0.7,
    "max_tokens": 1024,
    "thinking": "disabled"                     // enabled | disabled | auto,默认 auto
  }
}
```

`input` 为数组时是 OpenAI content parts:`[{"type":"text","text":"…"},
{"type":"image_url","image_url":{"url":"data:image/png;base64,…"}}]`,原样作为 user
message 的 content 转给 Chat 模型;`image_url` 走现有 `validate_chat_image_urls`
SSRF 校验(放行 `data:` 与公网 https)。

执行语义:

- `skill.content` 去掉 YAML frontmatter 后的正文 = **system message**;`input` = **user message**。
- frontmatter 只取 `name` 用于日志与响应;缺 frontmatter 合法(name 为 null)。
- 正文为空(去空白后)→ 422 `skill_empty`。
- 最终文本 = `choices[0].message.content` 去首尾空白。`reasoning_content`(reasoning
  parser 分离的思考)不返回、不注入。

响应 200:

```jsonc
{
  "text": "…最终 prompt…",
  "model": "qwen3-8-27b",
  "skill": { "name": "qwen-image-t2i" },
  "usage": { "prompt_tokens": 812, "completion_tokens": 143, "total_tokens": 955 },
  "finish_reason": "stop"
}
```

错误:

| 情形 | 状态 / code |
|---|---|
| 模型服务不存在 / key 无授权 | 404 `model_not_found` / 403 |
| 服务不是 `model` 类 | 400 `not_a_chat_model` |
| 模型未加载(含加载中) | 503 `model_not_ready`(**绝不在请求路径上加载**) |
| image_url 不安全 | 400 `unsafe_image_url` |
| SKILL 正文为空 | 422 `skill_empty` |
| 模型输出为空 | 502 `skill_empty_output` |
| `finish_reason == "length"` | 502 `skill_output_truncated`(截断的 prompt 绝不交给工作流) |
| 上游 vLLM 非 200 | 透出上游状态码与错误体(同 chat/completions) |
| 配额耗尽 | 402(同 chat/completions) |

### 3.2 `POST /v1/skill-runs/generate`

请求(`Prefer` 头语义与 predictions 完全一致:默认同步封顶 600s / `respond-async` →
202 / `wait=N`):

```jsonc
{
  "service": "qwen21-image-edit",
  "prompt_field": "prompt",
  "text": "…preview 返回(或用户修改后)的最终文本…",
  "input": { "image": "data:image/png;base64,…", "aspect_ratio": "1:1" },  // 其余字段原样透传
  "webhook": null,
  "webhook_events_filter": null
}
```

校验(在 predictions 既有的 schema 校验**之前**):

- `text` 去空白后非空,否则 422 `empty_text`。
- `prompt_field` 必须是该服务 input schema 里的属性,且 `type == "string"`,且不是文件类
  (`image`/`file`/`audio`/`video`/`binary`/`media`)。否则 422 `invalid_prompt_field`,
  错误体带 `string_fields: [...]`(该服务可用的文本字段),便于调用方自查。
- `input` 里已含 `prompt_field` → 422 `prompt_field_conflict`(来源歧义,不静默覆盖)。

之后合并 `{**input, prompt_field: text}`,**走与 `/v1/services/{name}/predictions` 同一个
提交实现**(schema 校验 + 动态 enum + 注入快照 + ExecutionTask + webhook)。

响应:与 predictions **完全相同的 Prediction 对象**;轮询 / SSE / 取消复用
`GET /v1/predictions/{id}`、`/stream`、`/cancel`。

### 3.3 鉴权与计量

- 与 chat / predictions 一致:Bearer M:N key 需对**对应服务**有 grant —— preview 看 `model`
  指的 Chat 服务,generate 看 `service` 指的工作流服务;受限流与配额约束。
- admin 会话(cookie 或 `ADMIN_TOKEN` bearer)旁路 grant/限流/配额,与现有 Playground 口径一致。
- preview 的 token 用量按普通 chat 记入该 Chat 服务(`record_llm_usage` + 配额扣减);
  generate 的计数与 predictions 一致(`usage_calls + 1`)。

## 4. 代码结构

新增(均**不出现** qwen / comfy / 节点 ID):

| 文件 | 职责 |
|---|---|
| `src/services/skill_run.py` | 纯逻辑无 I/O:解析 SKILL.md(frontmatter 解析提为公共函数 `parse_frontmatter`,`skill_manager` 改用它)、组 messages、从 chat 响应取最终文本并分类错误、按 service input schema 校验 `prompt_field` |
| `src/services/chat_invoke.py` | 从 `openai_compat.chat_completions` 抽出:`resolve_chat_target(...)`(服务解析 + readiness 503 + adapter)与非流式核心 `invoke_chat(...) -> ChatResult`(引擎引用护栏 C3、thinking 注入、max_tokens 夹紧、调 vLLM、usage 记录、配额扣减) |
| `src/services/prediction_submit.py` | 从 `predictions.create_prediction` 抽出:`submit_prediction(...)`(schema 校验 → 注入 → ExecutionTask → 同步/异步等待 → Prediction dict) |
| `src/api/service_access.py` | 服务解析 + 鉴权的共用函数(现 `predictions._resolve_service`、`openai_compat._admin_lookup_service` 等搬来),新路由不 import 别的路由模块的私有函数 |
| `src/api/routes/skill_runs.py` | 薄路由:鉴权 → 解析服务 → 调上面三块 |

修改:

- `openai_compat.chat_completions`:非流式分支改调 `invoke_chat`;流式分支保持原样,
  其前置解析与 readiness 改调 `resolve_chat_target`。行为不变。
- `predictions.create_prediction`:主体改调 `submit_prediction`。行为不变。
- `src/api/main.py`:注册 `skill_runs.router`。

护栏:

- `tests/test_data_plane_readonly.py`:静态锁定模块清单加 `skill_runs`(数据面对放置只读)。
- 新 `tests/test_skill_runs_generic.py`:静态断言 `skill_run.py` / `skill_runs.py` 源码不含
  `qwen`、`comfy`、`node_id`(不区分大小写)—— 把「不依赖特定服务/节点」锁进 CI。
- 重构后既有 chat / predictions 测试**一行不改**全部通过,作为回归网。

## 5. Qwen Image 2.1 接入

不写编排代码。步骤:

1. 核对模板数据:`GET /v1/services/{qwen21-text-to-image,qwen21-image-edit}/schema` 中
   `prompt` 为 `type: string`、编辑服务 `image` 为文件类(走桥的 data URL 上传)。现状
   (2026-09-26)已满足:t2i 暴露 `prompt/aspect_ratio/megapixels/seed`,edit 另加 `image`。
   映射有误走 `PUT /api/v1/comfy-templates/{id}/mapping` 修**数据**。
2. 两份示例 Skill(文生图 prompt 扩写;看原图写编辑指令),放
   `tests/manual/skill_runs/*.SKILL.md` —— 仅作端到端测试素材,不被任何代码加载;正式
   Skill 归调用方。
3. `docs/skill-runs.md`:给 nous-app 的接入说明(契约、错误码、preview → generate 两步
   流程、接入新类型工作流的方法)。

## 6. 测试

自动化(pytest,TDD,CI):

- `skill_run.py`:有/无 frontmatter、空正文、字符串与 content parts 输入、空输出、截断、
  `prompt_field` 缺失 / 非 string / 文件类 / 与 input 冲突。
- `/preview`:httpx mock vLLM —— 成功、503 未就绪、404 未知服务、400 非 model 服务、
  403 无 grant key、usage 记录、admin 旁路、unsafe image_url。
- `/generate`:mock runner,用一个**通用假 workflow 服务**且文本字段名为 `caption`
  (刻意不叫 `prompt`)跑同步 / `respond-async` / 各 422,证明与 Qwen 无关。
- 回归:既有 chat / predictions 全套。

端到端(真机、非 CI):`tests/manual/verify_skill_runs.py`,打生产 `:8000`:

- 文生图:preview(qwen3-8-27b + t2i Skill)→ generate `qwen21-text-to-image`
  `respond-async` → 轮询 succeeded → 确认有图片产物。
- 图像编辑:preview(input 带原图 `image_url`)→ generate `qwen21-image-edit`
  (`input.image` 为原图 data URL)→ 确认有图片产物。
- 跑前确认 ComfyUI 在线且无渲染进行中;产物落盘供人工目检。

## 7. 未来扩展(不在本期)

- 结构化输出映射:Skill 输出 JSON,按调用方给的 `{json_path → field}` 映射注入多个字段。
  届时 `generate` 增加可选 `fields` 映射,`text + prompt_field` 保持为其单字段特例。
