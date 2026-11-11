# MOSS 生图服务

服务名：`nous-moss-image`。复用已运行的 ComfyUI sidecar，属于 ComfyUI 桥服务。
导入模板和参数映射即可生成服务页面与 Playground，不需要修改后端、安装节点或重启服务。
注册服务不会执行工作流；真正调用时才会使用 DeepSeek 和 ComfyUI GPU 队列。

## 来源和执行链

来源：ComfyUI 用户工作流 `【WorkFisher】全能生图王 _ 双采.json`。
使用官方前端 1.57.4 的 `app.graphToPrompt()` 导出，转换期间拦截所有非 GET 请求。
原工作流文件未修改。

文字描述 → DeepSeek 扩写 → MOSS + LoRA → 原有采样链 → SeedVR2 超分 → 返回图片。
开启 `use_reference` 时改走原有图片反推分支，调用方必须同时传入 `reference_image`。
参考图仅用于提示词反推，不会作为 MOSS 的图像条件输入。

保留主模型 `moss_image_sft_bf16.safetensors`、文本编码器
`qwen3.5_9b_int8_convrot.safetensors`、`flux2-vae.safetensors`，以及原有采样器、CFG、
25 步采样、SeedVR2 7B Q8、2560 短边和块交换配置。
节点中的 `cuda:0` 是 sidecar 可见设备的逻辑索引，不改变 systemd 对 Pro 6000 的 GPU 绑定。

接入副本的处理：

1. 将原有四槽 LoRA 加载器接入模型与文本编码器链路，替代单独的观澜加载节点。
   第一槽默认为观澜、强度 1，第二至第四槽默认 `None`、强度 0。
2. LoRA 选项限定为 `None`、`moss/KOOK-MOSS-SFT-观澜.safetensors` 和
   `moss/TJFilmMossV2.safetensors`。这是本机 MOSS 目录下的候选，叠加效果仍需实测。
3. 懒切换直接读取扩写或反推结果，移除两个独立输出型文本预览节点，避免同时触发两路 API 请求。
4. 超分保存节点直接读取 SeedVR2 的第一个输出，修正原来指向对比器不存在的第二个输出的连线。
   服务仅保留最终图片保存节点，避免网关将超分前图片选为主图；原始 ComfyUI 工作流不受影响。
5. 未提供参考图时使用现有 `omit_when_empty` 机制删除图片加载节点及反推节点的可选 `source` 输入。
   默认走文字扩写，因此不会读取模板占位图片。

## 凭据和前置条件

DeepSeek 连接保持原有 endpoint、模型名 `deepseek-flash` 和本机配置项 `deepseek-flash`。
模板中的 `api_token` 为空，从 MieNodes 的 `mie_llm_keys.json` 读取凭据；密钥不进入仓库、
服务参数或调用方。服务固定使用 MieNodes 的 `MOSS/SKILL.md`。

本机已确认相关节点注册、两份 LoRA 文件名可被 ComfyUI 枚举、Skill 文件和凭据配置项存在。
修改上游模型名、凭据或 Skill 应走其原有维护流程。
图片反推还需要上游模型支持图像输入，当前只确认接线，未调用 DeepSeek 验证视觉能力。

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
服务详情中的 Playground 由 22 个映射参数生成，调用方也可直接获取上述 schema 渲染表单。

## nous 应用调用

应用后端持有授权给该服务的 API key，前端只提交业务参数。异步请求：

```http
POST /v1/services/nous-moss-image/predictions
Authorization: Bearer <service-api-key>
Prefer: respond-async
Content-Type: application/json

{"input":{"prompt":"雾中早市，纪实摄影","seed":42,"lora_01":"moss/KOOK-MOSS-SFT-观澜.safetensors","strength_01":1,"lora_02":"moss/TJFilmMossV2.safetensors","strength_02":0.5}}
```

拿到任务 ID 后，使用同一 API key 查询 `GET /v1/predictions/{id}`。
只在 `status=succeeded` 时读取 `output.image_url` 展示图片；失败展示任务 `error`。
图片反推需加 `use_reference:true` 和 `reference_image:"data:image/png;base64,..."`，
`reverse_instruction` 可修改反推要求，`prompt` 仍为必填字段。
LoRA 下拉选 `None` 或强度设为 0 均可停用该槽。

## 验证范围

已按运行中 ComfyUI 的节点 schema 检查必填输入、枚举和输出索引。
`tests/comfy/test_moss_image_template.py` 覆盖参数映射、缺省参考图剪枝、LoRA 接线、
懒分支和在隔离 PostgreSQL 测试库内导入后的服务 schema。
这些检查不启动推理进程、不调用 DeepSeek，也不向生产 ComfyUI 提交任务。

尚需发布后的端到端验证：默认观澜生图、TJFilmMossV2 单独及叠加生图、图片反推上游能力、
最终图片下载与 nous 应用展示。当前文档和产物不代表这些 GPU/外部 API 测试已通过。
