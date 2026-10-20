# nous ASR 对外输出契约 v1

`POST /v1/audio/transcriptions` 的响应契约。**与底层引擎无关**:任何 ASR 引擎
(Qwen3-ASR 已退役、MOSS-Transcribe-Diarize 现役、未来任何模型)的私有输出格式一律在
后端归一化到本契约,客户端永不因换引擎而改代码。2026-07 Qwen3→MOSS 换代已实证:
`text/language/usage` 零变动。

## 响应结构

```json
{
  "text": "纯文本转写全文",
  "language": "zh" ,
  "usage": {"type": "duration", "seconds": 8},
  "segments": [
    {"start": 0.26, "end": 3.24, "speaker": "S01", "text": "该段纯文本"}
  ]
}
```

## 字段承诺(客户端可依赖的不变量)

| 字段 | 承诺 |
|---|---|
| `text` | **永远存在、永远第一字段、永远纯文本**(无时间戳/说话人 markup)。只读 `text` 的最简客户端在任何引擎下都成立。 |
| `language` | 永远存在。取值 = **引擎提供的语种,或引擎不提供时由后端文本字符集检测**(`_detect_language`,zh/ja/ko/ru/en,ISO 639-1 码);引擎与检测均无法判定才为 `null`(verbose_json 格式下为 `"und"`),**不会缺字段**。 |
| `usage` | 永远 `{"type":"duration","seconds":int≥1}`,秒 = 音频真实时长(计费口径),与引擎无关(后端自算)。 |
| `segments` | 仅当请求 `timestamps=true` 时出现。每段:`start`/`end` 秒(float,单调不减)、`speaker`(`"S01"` 式标签,引擎无说话人分离能力时为 `null`,**不会缺字段**)、`text` 纯文本。 |

## 入参契约(同样引擎无关)

`file`(multipart,任意常见音频格式,后端 ffmpeg 归一化)、`model`(服务名)、
`timestamps`(bool,要分段)、`context`(热词/领域偏置,引擎不支持时静默忽略)、
`language`(OpenAI 兼容保留位,当前不消费)、`response_format`(**已消费**,见下节)、
`merge_segments`(bool,默认 `false`,**已消费**,见下)、
`punctuate`(bool,默认 `true`,**已消费**,见下)。

`merge_segments`:段级后处理开关。`true` 时后端**确定性贪心合并**碎分段成句子级
(适合字幕/阅读场景:MOSS 分段跟内容节奏走,快节奏口播被切到 ~1.9s/段,太碎)。
合并规则:只并同 `speaker`(均 `null` 视为同)且相邻间隔 ≤ 0.8s 的段;组文本以句末标点
(。!?!?…)收尾且组时长 ≥ 3s、或组时长 > 15s、或组字数 > 80、或 speaker 变化/间隔超阈
即封组。合并后段 = `{start=组首 start, end=组尾 end, speaker, text=拼接}`。默认 `false` 时
既有输出**零变化**(只加不改);`true` 只改变 `segments`(默认格式 + verbose_json 一处生效),
`text` 全文不变(全文本来就是全段拼接)。

`punctuate`:快语速标点恢复兜底,**默认 `true`,自动触发**。快速连续/无停顿口播下 MOSS
有时整篇输出零标点(prompt steer 无效,真机实测:94s 教程音频全文 0 标点),影响可读性。
后端在转写后检测**标点密度**(标点数/字符数,标点集 = 中英逗/句/问/叹/顿/分号):当
`len(text) ≥ 80` **且** 密度 `< 0.005` 时,自动用本机常驻 LLM 做**纯标点恢复**——把各段编成
`<n>|<text>` 行喂 LLM,只允许加标点。**严格不改字保证**:回填前逐行校验,剥掉标点后必须与
原文逐字相等,不等的行保留原文;LLM 返回行数/编号对不上则整体放弃。作用在 `merge_segments`
**之前**(合并断句依赖句末标点),对顶层 `text` 与 `segments` 均生效(`timestamps=false` 时也
补顶层 `text`)。LLM 未加载 / 不可达 / 校验失败 → 静默降级用原文(标点恢复是**质量增强、非
结构变更**,契约「只加不改」允许,且绝不拖垮主路、绝不按需拉起 LLM)。`punctuate=false` 显式关闭;
密度已达标(正常语速音频)时不触发,既有输出零变化。

## 长音频处理

超过 10 分钟的归一化音频按 PCM 帧顺序拆成最多 600 秒一段，逐段请求同一 MOSS
服务。每段保留原有按时长分配的输出预算（最长段 19,712 tokens），不提高模型显存
比例、不切换 GPU，也不靠压低整段输出上限截断全文。短音频保持单次请求。

分段结果按顺序拼接，`start/end` 加上各段起点以还原原音频时间轴；所有音频帧都参与
转录，不省略尾段。时间边界是固定切分，不保证落在静音处，边界附近的识别质量仍受
模型影响。各段的说话人标签独立识别，返回时重编号为不重用的 `S01/S02/…`，避免把
不同段的同名标签当作同一个人；**不同标签不证明是不同真人，不提供跨段身份匹配**。

一次外部调用仍只创建一条任务，`usage.seconds` / `duration` 按完整音频计算一次。
任一段失败时整次请求失败，不把已有部分返回为成功，不自动重试或静默降级到其他
引擎；取消中断后不再发送后续分段。分段只限定单次推理上下文，完整上传和归一化仍
在内存中，内存用量会随原音频长度增长。

## 演进规则

1. **只加不改**:新能力 = 新增字段/新增可选入参;既有字段的名字、类型、语义、
   nullability 永不变。破坏性变更 = 新版本路径,不复用 v1。
2. **能力降级显式化**:引擎缺某能力 → 对应字段置 `null`(language/speaker)或不出现
   (segments),绝不改变结构或塞私有格式进 `text`。
3. **新引擎接入清单**:实现一个归一化函数(参照 `openai_compat.py` 的
   `_asr_moss_transcribe`,返回 `(text, language, segments)` 三元组)+ 对照本契约过一遍
   字段承诺 + `tests/test_asr_transcription.py` 加该引擎的 mock 用例。引擎私有协议
   (如 Qwen3 的 `<asr_text>` 标记、MOSS 的 `[Sxx]` 前缀)**只允许存在于归一化函数内部**。
4. 严格 OpenAI-Whisper `verbose_json` 兼容:经 `response_format=verbose_json`
   走**独立出参分支**映射(我们的 segments → Whisper 段形状),**不动本契约默认输出**。
   **已实现**(2026-07-21,PR-7),见下节。

## `response_format` 出参格式(§4 演进钩子的实现)

`response_format` 是入参,选 `POST /v1/audio/transcriptions` 的出参形状。默认输出(上文
「响应结构」)一字不动;`verbose_json` 是**另一条分支**,供 OpenAI-Whisper 生态 SDK
(openai-python 等)直连。未知取值 → 400。

| `response_format` | 出参 | 分段 | 用途 |
|---|---|---|---|
| 缺省 / `json` | 本契约默认:`{text, language, usage}`(+ `timestamps=true` 时 `segments`) | 由 `timestamps` 门控 | nous 客户端;段带 `speaker`(平台增强) |
| `text` | 纯文本 body(`text/plain`),无 JSON 包裹、无 `language`/`usage`/`segments` | 无 | OpenAI `text` 语义,只要转写全文 |
| `verbose_json` | OpenAI-Whisper 段形状(见下) | **隐含**(始终带 `segments`,无需另传 `timestamps`) | OpenAI SDK 直连、Whisper 生态工具 |

`verbose_json` 形状:

```json
{
  "task": "transcribe",
  "language": "zh",
  "duration": 8.0,
  "text": "纯文本全文",
  "segments": [
    {"id": 0, "seek": 0, "start": 0.26, "end": 3.24, "text": "段纯文本",
     "tokens": [], "temperature": 0.0, "avg_logprob": 0.0,
     "compression_ratio": 1.0, "no_speech_prob": 0.0, "speaker": "S01"}
  ]
}
```

- `language`:引擎或后端检测得出;均无 → `"und"`(OpenAI 语义:未定语种;不为 null)。
- `duration`:float 秒 = 后端自算的 `audio_seconds`(与 `usage.seconds` 同一口径;归一化
  wav 算出,不依赖引擎透传时长)。
- `segments[].{seek,tokens,temperature,avg_logprob,compression_ratio,no_speech_prob}`:
  **中性占位** —— MOSS 不产这些 token 级/逐段置信度指标,给 OpenAI SDK 惯例默认值(字段
  在、值中性),SDK 不因缺字段报错。
- `segments[].speaker`:nous **附加字段**(OpenAI schema 无此键),SDK 忽略未知字段;无
  说话人归属的段为 `null`。

## 历史

- 2026-06-21:v1 雏形(Qwen3-ASR:text/language/usage + words 字级时间戳)。
- 2026-07-20:`words`(字级,对齐器)→ `segments`(段级+speaker,MOSS 内建)。
  这是唯一一次破坏性变更,随对齐器退役发生;此后按演进规则冻结。
- 2026-07-21:本契约文档化,spec `2026-07-20-moss-asr-sglang-serving-design.md` §3 为实现出处。
- 2026-07-21(PR-7):§4 演进钩子落地 —— `response_format` 开始消费(`json`/`text`/`verbose_json`,
  未知值 400);新增 OpenAI-Whisper `verbose_json` 出参分支(独立映射,默认输出不变);
  `language` 语义扩展为「引擎提供或后端文本字符集检测,均无才 null / und」。纯新增,默认契约冻结不动。
- 2026-07-21(PR-8):新增可选入参 `merge_segments`(bool,默认 `false`)—— `true` 时服务端确定性
  贪心合并碎分段成句子级(字幕/阅读场景),作用于 `segments`、默认格式 + verbose_json 一处生效;
  `text` 全文不变。纯新增可选入参,默认关、既有输出零变化。
- 2026-07-22(PR-10):新增可选入参 `punctuate`(bool,默认 `true`,自动兜底)—— 快语速 MOSS 输出
  零标点时(`len(text) ≥ 80` 且标点密度 `< 0.005`)自动用本机常驻 LLM 做纯标点恢复,校验后回填
  (剥标点逐字相等才采用,严格不改字),在 `merge_segments` 之前生效;LLM 不可达/校验失败静默降级
  用原文。加标点属质量增强(只加不改),既有字段结构/语义不变;正常语速音频密度达标不触发。
