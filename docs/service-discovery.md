# 服务发现(discovery)与第一阶段原子图像服务

调用方(nous-app)不认服务名也能区分「文生图 / 图像编辑 / 图片放大」。协议只有两处读取:

| 端点 | 鉴权 | 用途 |
|---|---|---|
| `GET /v1/services[?operation=…]` | Bearer M:N key(只列被授权的)/ admin | 目录:有哪些可发现服务 |
| `GET /v1/services/{name}/schema` | 公开 | 单个服务的调用契约(**参数唯一真相**),多一个 `discovery` 块 |

调用仍走现有的 `POST /v1/services/{name}/predictions`、`GET /v1/predictions/{id}`、
`POST /v1/predictions/{id}/cancel`、`POST /v1/skill-runs/{preview,generate}`,没有第二套协议。

## discovery 块

```json
{
  "operation": "text_to_image",          // text_to_image | image_edit | image_upscale
  "display_name": "Qwen Image 2.1 文生图",
  "supports_lora": true,
  "lora_slots": 8,
  "media_inputs": [],                    // [{key, type, required}],按参考图顺序
  "prompt_fields": ["prompt"]            // skill-runs/generate 可写的字段;空 = 不是 Skill 目标
}
```

没声明 discovery 的服务:schema 里 `discovery: null`,且不进目录。目录每项 = discovery 块 +
`service`(调用用的名字,别显示给最终用户)+ `outputs` + `schema_url`。

**存的只有** `operation / display_name / lora_slots`(`service_instances.discovery`),随
comfy 模板的 `PUT /api/v1/comfy-templates/{id}/mapping` 顶层 `discovery` 键写入:省略 = 保留
已存值(并复核与新 mapping 一致),显式 `null` = 清除。其余字段从 exposed_inputs 推导。校验与
公开视图的唯一实现:`backend/src/services/service_discovery.py`。

## LoRA 槽约定

`lora_slots = N` ⇔ mapping 恰有 `lora_1 … lora_N` 与 `lora_i_strength`;顺序 1 → N 即加载顺序。
`lora_i` 是封闭白名单(含 `"None"` = 空槽),选项只能是模型目录下的相对文件名(拒绝绝对路径、
盘符、反斜杠、`..`),调用期由 schema 的 `enum` 拦住任何其它值。选项的友好名在 schema 的
`x-option-meta[].label`。未满 N 个 → 其余槽传 `"None"`(或不传,默认即 `"None"`),强度用默认值。

## 第一阶段四个服务

| 服务 | operation | 必填 | 其余参数 | 产物目录 |
|---|---|---|---|---|
| `nous-qwen21-text-to-image` | text_to_image | prompt | aspect_ratio, megapixels, seed, lora_1..8(+_strength,-10…10) | `docs/replications/qwen21-text-to-image/` |
| `nous-qwen21-image-edit` | image_edit | prompt, image | image2..image8(可选,按序), aspect_ratio, megapixels, seed | `docs/replications/qwen21-image-edit/` |
| `nous-seedvr2-image-upscale` | image_upscale | image | scale_multiplier, seed, sampler_name, scheduler, denoise, color_correction_method | `docs/replications/upscale/` |
| `nous-vosr2-image-upscale` | image_upscale | image | upscale, seed, color_alignment, tile_size, tile_overlap, vae_tile_size, vae_tile_overlap, dtype | `docs/replications/upscale/` |

四个都输出 `image_url`。模型加载器、输出目录不在 mapping 里,调用方传了也不会写进图。
Skill 在 skill-runs 层执行,ComfyUI 只收到最终 prompt 与图片;图像编辑不带 LoRA。

文生图 LoRA:两个串联的 `Lora Loader Stack (rgthree)`(各 4 槽),一个 strength 同时作用于
MODEL/CLIP(节点原生语义)。当前 ComfyUI 上与 Qwen Image 2.1 兼容的只有
`Qwen/Qwen-Image-2.1-viggle-turbo-4step-lora-r64.safetensors`;新增 LoRA = 往 8 个 `lora_i`
的 options 里加一项再跑 deploy.sh。
