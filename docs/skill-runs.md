# skill-runs:Skill 驱动的工作流编排

把「用 Chat 模型执行一段 Skill → 得到一段文本 → 注入任意已发布工作流的文本字段 → 提交」标准化。
设计见 `docs/superpowers/specs/2026-09-26-skill-runs-orchestration-design.md`。

## 两步流程

1. `POST /v1/skill-runs/preview` —— 拿到最终文本(可展示给用户修改)。
2. `POST /v1/skill-runs/generate` —— 把文本写进工作流的 `prompt_field` 提交,返回 Prediction;
   用 `GET /v1/predictions/{id}` 轮询(或 `/stream`、`/cancel`)。

鉴权与 `/v1/chat/completions`、`/v1/services/{name}/predictions` 相同:M:N key 需对 Chat 服务
(preview)与工作流服务(generate)**分别**有 grant。

## preview

    curl -s http://<engine>:8000/v1/skill-runs/preview \
      -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{
        "model": "qwen3-8-27b-twolven",
        "skill": {"content": "---\nname: my-skill\n---\n把想法扩写成画面描述,只输出描述。"},
        "input": "黄昏窗台上的猫",
        "options": {"max_tokens": 1024, "thinking": "disabled"}
      }'
    → {"text": "...", "model": "qwen3-8-27b-twolven", "skill": {"name": "my-skill"},
       "usage": {...}, "finish_reason": "stop"}

- `skill.content`:完整 SKILL.md。frontmatter 可选(只读 `name`);正文作为 system 指令。
- `input`:字符串,或 OpenAI content parts(`text` + `image_url`,支持 `data:` 与公网 https),
  给视觉模型看图用(如图像编辑类 Skill)。**只收这两种 part**:`{"type":"text","text":<str>}`、
  `{"type":"image_url","image_url":{"url":<str>}}`;其余(`video_url`、`audio_url`、缺 `type`、
  字段形状不对)→ 422 `invalid_input_part`。
- 输入上限:`input` 字符串 ≤ 100,000 字符,content parts ≤ 32 个;超限 → 400 `validation_error`。
- `options.temperature`:0–2,缺省用模型默认。
- `options.max_tokens`:>0,缺省 2048,并按模型 `max_model_len` 夹紧。
- `options.thinking`:`enabled` | `disabled` | `auto`,默认 `disabled`
  (思考 token 会占 `max_tokens`,容易截断)。

## generate

    curl -s http://<engine>:8000/v1/skill-runs/generate \
      -H "Authorization: Bearer $KEY" -H 'Prefer: respond-async' -H 'Content-Type: application/json' -d '{
        "service": "qwen21-image-edit",
        "prompt_field": "prompt",
        "text": "<preview 返回的 text>",
        "input": {"image": "data:image/png;base64,..."}
      }'
    → 202 + Prediction(与 predictions 完全相同)

- `prompt_field` 必须是该服务的**自由文本**输入(string、非文件、无选项清单);
  可选值见 `GET /v1/services/{service}/schema`,选错时错误体 `error.text_fields` 列出可用字段。
- `input` 里不能再带 `prompt_field`(422 `prompt_field_conflict`)。其余字段照 schema 校验。
- `text` ≤ 100,000 字符;超限 → 400 `validation_error`。
- `Prefer`:缺省同步(封顶 600s)/ `respond-async`(202)/ `wait=N`。

## 错误码

除一种情况外,错误统一包在 `{"error": {message, type, code?, param?, ...}}` 信封里,下表的 code
即 `error.code`(服务解析阶段的 404/403/402 带默认 code)。**例外**:preview 时上游 vLLM 返回
非 200,引擎原样透出上游的状态码与原始响应体(同 `/v1/chat/completions`),不套信封。

| code | HTTP | 含义 |
|---|---|---|
| (默认) | 404 | 服务不存在,或 M:N key 对该服务没有 grant |
| (默认) | 403 | 服务未激活(inactive) |
| (默认) | 402 | 该 key 的配额已耗尽 |
| `model_not_ready` | 503 | Chat 模型未加载(引擎不会在请求路径上加载;见 `GET /v1/models`) |
| `not_a_chat_model` | 400 | preview 的 `model` 不是 Chat 模型服务 |
| `unsafe_image_url` | 400 | `image_url` 指向私网/回环/非 https |
| `skill_empty` / `skill_invalid_frontmatter` / `empty_input` | 422 | 请求内容不可用 |
| `invalid_input_part` | 422 | content part 不是 `text` / `image_url`,或字段形状不对 |
| `skill_output_truncated` | 502 | 输出被 `max_tokens` 截断 —— 调大或关思考 |
| `skill_empty_output` | 502 | 模型没有输出文本 |
| `invalid_prompt_field` / `prompt_field_conflict` / `empty_text` | 422 | generate 字段问题 |
| `validation_error` | 400 | 请求体结构/取值不合法 |

## 接入新的工作流(视频 / 音频 / 3D …)

不需要改引擎代码。把工作流发布成服务(画布发布或 ComfyUI 模板 + 映射),确保它暴露一个
string 类型的文本输入;调用方把那个 key 作为 `prompt_field` 即可。节点映射属于模板数据
(`PUT /api/v1/comfy-templates/{id}/mapping`),不属于编排。

## 边界

只支持「Skill 输出一段文本 → 注入一个字段」。结构化输出(同时产出 negative prompt、比例、
镜头参数…)到时另开 spec。
