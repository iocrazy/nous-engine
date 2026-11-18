# YZ 金鱼 Qwen Image 2.1 多合一

服务名为 `nous-qwen21-all-in-one`。来源为 ComfyUI 的 `YZ金鱼-Qwen_image_2.1-超强多合一_v2.json`。Playground 使用与 `nous-moss-image` 相同的单选控件。每次执行一个模式并返回一张图片。

| 模式 | 输入与行为 |
| --- | --- |
| `text` 文字生图 | 输入提示词。可关闭 Nous Engine 的本机 Qwen 提示词优化。 |
| `reference` 图片反推 | 输入一张图片，用 Nous Engine 的本机 Qwen 视觉服务反推并重新生图。可附加提示词。 |
| `edit` 单图／多图编辑 | 输入编辑要求和 1–6 张参考图。支持调整 LoRA、使用首图的透明遮罩和编辑后放大。提示词优化默认用本机 Qwen，可切换 DeepSeek 或关闭。 |
| `outpaint` 扩图 | 输入一张图片，按原图尺寸的百分比分别设置四边扩展，可输入补充提示词。 |
| `remove` 遮罩移除 | 上传原图和同尺寸黑白 PNG 遮罩。白色为移除区域，黑色为保留区域。 |
| `upscale` 仅图片放大 | 上传一张图，原图直接进入 SeedVR2。 |

保留原工作流的 Turbo BF16 主模型、文本编码器、VAE、采样器和 sigma 序列。编辑 LoRA 默认只启用一致性模型，其他可用原图 LoRA 从下拉列表选用。参考图 2–6 预缩放至 1.5 MP，留空时由桥剪掉对应加载器及连线。编辑后放大保留原图中的 0.3 MP 预缩放。放大短边默认 2560。模板只从服务端配置读取 LLM 凭据。

## 注册

先部署支持映射字段 `required_when` 的 Nous Engine 版本。该字段让引擎在排队前验证当前模式所需的文件和提示词。取得上线授权后，从本目录运行以下命令。注册复用当前 ComfyUI 实例，无需重启 ComfyUI。

```bash
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8000}
: "${ADMIN_TOKEN:?需要管理令牌}"
auth=(-H "Authorization: Bearer $ADMIN_TOKEN")

created=$(jq -n --slurpfile wf qwen21-all-in-one.api.json \
  '{name:"nous-qwen21-all-in-one",output_kind:"image",workflow:$wf[0]}' \
  | curl --noproxy '*' --fail-with-body -sS "${auth[@]}" -H 'Content-Type: application/json' \
      -X POST "$BASE/api/v1/comfy-templates" --data-binary @-)
id=$(jq -er '.id' <<<"$created")
template="$BASE/api/v1/comfy-templates/$id"
schema="$BASE/v1/services/nous-qwen21-all-in-one/schema"

if ! curl --noproxy '*' --fail-with-body -sS "${auth[@]}" -H 'Content-Type: application/json' \
      -X PUT "$template/mapping" --data-binary @qwen21-all-in-one.mapping.json \
  || ! curl --noproxy '*' --fail-with-body -sS "$schema" \
      | jq -e '.input_schema.properties.mask["x-required-when"].input_mode == "remove"
                and (.output_schema.properties | has("image_url"))'; then
  curl --noproxy '*' --fail-with-body -sS "${auth[@]}" -X DELETE "$template"
  exit 1
fi
```

调用 `POST /v1/services/nous-qwen21-all-in-one/predictions`，请求头加 `Prefer: respond-async`，例如 `{"input":{"input_mode":"text","prompt":"薄雾中的山谷"}}`。使用同一服务密钥查询 `GET /v1/predictions/{id}`。成功时图片在 `output.outputs.out.image_url`。上传字段支持图片 data URI。
