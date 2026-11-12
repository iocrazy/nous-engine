# MOSS 生图服务

服务名：`nous-moss-image`。复用已运行的 ComfyUI sidecar，属于 ComfyUI 桥服务。
导入模板和参数映射即可生成服务页面与 Playground，不需要修改后端、安装节点或重启服务。
注册服务不会执行工作流；真正调用时才会使用 DeepSeek 和 ComfyUI GPU 队列。

## 来源和执行链

来源：ComfyUI 用户工作流 `【WorkFisher】全能生图王 _ 双采.json`。
使用官方前端 1.57.4 的 `app.graphToPrompt()` 导出，转换期间拦截所有非 GET 请求。
原工作流文件未修改。

文字描述 → 可选 DeepSeek 扩写 → MOSS + 可选 LoRA → 原有采样链 → 可选 SeedVR2 超分 → 返回图片。
页面通过“输入方式”单选框二选一：文字生图或图片反推。
选择反推（`use_reference=true`）时改走原有图片反推分支，调用方必须同时传入 `reference_image`。
参考图仅用于提示词反推，不会作为 MOSS 的图像条件输入。

保留主模型 `moss_image_sft_bf16.safetensors`、文本编码器
`qwen3.5_9b_int8_convrot.safetensors`、`flux2-vae.safetensors`，以及原有采样器、CFG、
25 步采样、SeedVR2 7B Q8、2560 短边和块交换配置。
节点中的 `cuda:0` 是 sidecar 可见设备的逻辑索引，不改变 systemd 对 Pro 6000 的 GPU 绑定。

接入副本的处理：

1. 将原有四槽 LoRA 加载器接入模型与文本编码器链路，替代单独的观澜加载节点。
   第一槽默认为观澜、强度 1、开启，第二至第四槽默认 `None`、强度 1、关闭。
   每槽的独立开关通过懒切换将关闭槽的有效强度置为 0，LoRA 加载器跳过该槽，保存的文件名和强度不变。
2. LoRA 选项限定为 `None`、`moss/KOOK-MOSS-SFT-观澜.safetensors` 和
   `moss/TJFilmMossV2.safetensors`。这是本机 MOSS 目录下的候选，叠加效果仍需实测。
3. 懒切换直接读取扩写或反推结果，移除两个独立输出型文本预览节点，避免同时触发两路 API 请求。
4. 超分支路直接读取 SeedVR2 的第一个输出，修正原来指向对比器不存在的第二个输出的连线。
   服务仅保留一个终端保存节点，按开关选择原图或超分图，避免网关将其他支路图片选为主图；
   原始 ComfyUI 工作流不受影响。
5. 未提供参考图时使用现有 `omit_when_empty` 机制删除图片加载节点及反推节点的可选 `source` 输入。
   默认走文字扩写，因此不会读取模板占位图片。

## 独立开关

| 参数 | 默认 | 关闭后的行为 |
|---|---|---|
| `enable_expansion` | true | 文字生图直接使用 `prompt`，跳过 DeepSeek 文字扩写 |
| `enable_upscale` | true | 直接保存 MOSS 原始分辨率图片，不执行 SeedVR2 加载与超分支路 |
| `enable_lora_01` | true | 跳过第 1 槽 LoRA |
| `enable_lora_02` | false | 跳过第 2 槽 LoRA |
| `enable_lora_03` | false | 跳过第 3 槽 LoRA |
| `enable_lora_04` | false | 跳过第 4 槽 LoRA |
| `use_reference` | false | 输入方式：false 为文字生图，true 为图片反推 |

LoRA 槽要同时满足开关开启、文件非 `None`、强度非 0 才生效。
关闭再开启会恢复之前所选的文件和强度；新增的第 2–4 槽需明确打开开关。
两种输入方式互斥。文字模式显示提示词和文字扩写选项；反推模式只显示参考图与反推要求。
扩写参数只在文字模式且扩写开启时显示。不属于当前模式的字段不提交，但切换回来会恢复之前填写的值。
接口通过布尔字段 `use_reference` 选择分支：true 时走 DeepSeek 图片反推，`enable_expansion` 不影响它。
只有 `enable_expansion=false` 且 `use_reference=false` 时完全跳过 DeepSeek。
这些开关使用现有 `LazySwitchKJ` 节点，不需要新增节点包或修改引擎运行时代码。

## 凭据和前置条件

DeepSeek 连接保持原有 endpoint、模型名 `deepseek-flash` 和本机配置项 `deepseek-flash`。
模板中的 `api_token` 为空，从 MieNodes 的 `mie_llm_keys.json` 读取凭据；密钥不进入仓库、
服务参数或调用方。服务固定使用 MieNodes 的 `MOSS/SKILL.md`。

本机已确认相关节点注册、两份 LoRA 文件名可被 ComfyUI 枚举、Skill 文件和凭据配置项存在。
修改上游模型名、凭据或 Skill 应走其原有维护流程。
本机 `deepseek-flash` 已实测可接收参考图并返回反推描述，见下方真机验证记录。

## 注册

这是控制面写操作。在获得发布确认后，在本目录执行，`ADMIN_TOKEN` 通过环境变量提供：

```bash
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8000}
: "${ADMIN_TOKEN:?需要管理令牌}"

created=$(jq -n --slurpfile wf moss-image.api.json \
  '{name:"nous-moss-image",output_kind:"image",workflow:$wf[0]}' \
  | curl --noproxy '*' --fail-with-body -sS \
    -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
    -X POST "$BASE/api/v1/comfy-templates" --data-binary @-)
tid=$(jq -er '.id' <<<"$created")

if ! curl --noproxy '*' --fail-with-body -sS \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -X PUT "$BASE/api/v1/comfy-templates/$tid/mapping" \
  --data-binary @moss-image.mapping.json; then
  curl --noproxy '*' --fail-with-body -sS -H "Authorization: Bearer $ADMIN_TOKEN" \
    -X DELETE "$BASE/api/v1/comfy-templates/$tid"
  exit 1
fi

curl --noproxy '*' --fail-with-body -sS "$BASE/v1/services/nous-moss-image/schema"
```

新建同名服务会返回 409，不覆盖任何已存在服务。回滚仅删除本次新建的模板，会级联删除其服务。
导入后服务位于“全部 / ComfyUI 桥 / 其他”；现有 ComfyUI 模板的分类为 `app`。
服务详情中的 Playground 由 28 个映射参数生成，调用方也可直接获取上述 schema 渲染表单。

## nous 应用调用

应用后端持有授权给该服务的 API key，前端只提交业务参数。异步请求：

```http
POST /v1/services/nous-moss-image/predictions
Authorization: Bearer <service-api-key>
Prefer: respond-async
Content-Type: application/json

{"input":{"prompt":"雾中早市，纪实摄影","seed":42,"lora_01":"moss/KOOK-MOSS-SFT-观澜.safetensors","strength_01":1,"enable_lora_02":true,"lora_02":"moss/TJFilmMossV2.safetensors","strength_02":0.5}}
```

拿到任务 ID 后，使用同一 API key 查询 `GET /v1/predictions/{id}`。
只在 `status=succeeded` 时读取 `output.outputs.out.image_url` 展示图片；失败展示任务 `error`。
`output.outputs.bridge` 是桥节点的同一份结果，应用应统一读取终端节点 `out`。
若图片地址为相对路径，使用引擎地址作为基址解析后再下载。
图片反推需加 `use_reference:true` 和 `reference_image:"data:image/png;base64,..."`，
`reverse_instruction` 可修改反推要求，反推模式不需要提供 `prompt`。
文字描述字段允许留空并默认使用空字符串，避免未参与反推支路的输入被桥接层误判为必填。
映射中的 `visible_when` 声明字段在什么布尔参数取值下显示，API schema 同时输出 `x-visible-when`。
LoRA 下拉选 `None` 或强度设为 0 均可停用该槽。
直接使用原提示词、关闭全部增强的请求示例：

```json
{"input":{"prompt":"窗边木桌上的红色陶瓷茶壶，静物摄影","enable_expansion":false,"enable_upscale":false,"enable_lora_01":false,"enable_lora_02":false,"enable_lora_03":false,"enable_lora_04":false}}
```

## 验证范围

已按运行中 ComfyUI 的节点 schema 检查必填输入、枚举和输出索引。
`tests/comfy/test_moss_image_template.py` 覆盖参数映射、缺省参考图剪枝、LoRA 接线、
懒分支和在隔离 PostgreSQL 测试库内导入后的服务 schema。
开关测试覆盖文字扩写、超分、图片反推的 8 种组合，以及四个 LoRA 开关的全部 16 种组合。
这些检查不启动推理进程、不调用 DeepSeek，也不向生产 ComfyUI 提交任务。

## 真机验证（2026-11-11）

在用户授权测试后，通过控制面创建临时服务和临时授权 key，以实际外部调用路径提交任务。
使用现有 ComfyUI sidecar 和 Pro 6000，不重启服务、不调整 GPU 0 常驻模型。
四次任务均使用原有 25 步采样、2 百万像素生图和 2560 短边超分配置。

| 测试 | LoRA 设置 | 任务耗时 | 结果 |
|---|---|---:|---|
| 文字扩写与生图 | 观澜 1.0 | 92.21 秒 | 成功，4558×2560 PNG |
| 替换 LoRA | TJFilmMossV2 1.0 | 42.06 秒 | 成功，4558×2560 PNG |
| 叠加 LoRA | 观澜 1.0 + TJFilmMossV2 0.5 | 44.10 秒 | 成功，4558×2560 PNG |
| 图片反推与生图 | 观澜 1.0，使用第一组结果作为参考图 | 72.67 秒 | 成功，4558×2560 PNG |

耗时为提交请求到轮询检测任务完成，不含图片下载时间；每组仅测一次。
第一组包含 DeepSeek 扩写和首次模型加载，第二、第三组复用了相同扩写缓存，不能直接当作独立冷启动速度。
日志确认文字扩写只调用一次 DeepSeek，耗时 23.52 秒；图片反推调用一次，耗时 26.16 秒。
四张图片均成功下载、解码并检查画面，测试时未发现 LoRA 加载错误或 OOM。
这些结果验证调用链和本次配置可用，不代表所有题材的 LoRA 质量或长期并发稳定性。

另用临时服务在真实浏览器中打开生产 Playground，确认 LoRA 2 可以选择 TJFilmMossV2，
强度可设为 0.5，页面存在参考图上传控件。浏览器测试没有额外提交生图请求。
测试结束后，临时服务、模板和授权 key 均删除成功；没有正式发布 `nous-moss-image`。

本机原始记录和图片在 `/tmp/nous-moss-service/live-test/`，其中 `results.json` 为任务结果，
`ui-result.json` 为浏览器测试结果，`playground.png` 为界面截图。
尚需接入 nous 应用的任务提交、进度查询和图片展示；该应用页面未在本次测试中修改。
