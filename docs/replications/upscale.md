# 放大服务复刻 manifest(SeedVR2 图片 / VOSR2 图片 / VOSR2 视频)

日期: 2026-09-27 | 方式: **ComfyUI 桥(模板即服务)** | 产物目录: `docs/replications/upscale/`

| 服务名 | 模板产物 | 输出 | 来源工作流 |
|---|---|---|---|
| `nous-seedvr2-image-upscale` | `seedvr2-image-upscale.{api,mapping}.json` | image | `SeedVR2-7B-独立图片放大.json` |
| `nous-vosr2-image-upscale` | `vosr2-image-upscale.{api,mapping}.json` | image | `全能图像+视频修复放大【VOSR2】.json`(只开图片分支) |
| `nous-vosr2-video-upscale` | `vosr2-video-upscale.{api,mapping}.json` | video | 同上(只开视频分支) |

## UI → API 转换:官方前端,不是 comfy-cli

SeedVR2 的外层节点 66 是**子图**,展开后的节点 ID(`66:54` 这种)只能以官方前端为准。
`convert.cjs` 用 playwright 无头打开 sidecar 页面,在页面里 `app.loadGraphData()` +
`app.graphToPrompt()`,**拦截所有非 GET 请求**(不会入队、不写 settings/userdata)。
VOSR2 两个变体在加载前改节点 `mode`(0=启用,4=bypass),见 `variants.json`;同时把
SaveImage / VHS_VideoCombine 的 `filename_prefix` 改成 `nous-vosr2-{image,video}`。

```bash
node docs/replications/upscale/convert.cjs docs/replications/upscale/variants.json /tmp/out
comfy validate --workflow /tmp/out/<name>.api.json --input object_info.json
```

三份都 `valid: true, 0 errors`;SeedVR2 的 3 条 `COMFY_MATCHTYPE_V3` warning 是已知误报。
重跑转换与提交的产物逐字节一致。

## 参数契约

- 模型权重**不暴露**:SeedVR2 固定 `seedvr2_7b_int8_convrot.safetensors` +
  `seedvr2_ema_vae_fp16.safetensors`,VOSR2 固定 `VOSR2`。桥只改 mapping 里声明的
  `(comfy_node_id, comfy_input)`,调用方多传的键被忽略(`tests/comfy/test_upscale_services.py`)。
- SeedVR2 参数落在子图展开后的节点:`scale_multiplier → 66:57.resize_type.multiplier`、
  `seed/sampler_name/scheduler/denoise → 66:54`、`color_correction_method → 66:59`。
- VOSR2 视频**默认保留原帧率**:这由工作流自身表达 —— `VHS_VideoCombine.frame_rate` 连到
  `VHS_VideoInfo` 的 `loaded_fps`(槽 5),`force_rate` 默认 0(不重采样)。适配层无需特判。
- `frame_load_cap` 默认 **150**、范围 **1–600**(0 在 VHS 里是「全部帧」,等于绕过上限,不开放);
  `skip_first_frames` ≤ 10000。VHS 把帧以 float32 全放主机 RAM:2560×1440 输出约 44MB/帧,
  900 帧就是 ~40GB,正是 2026-09-11 那类主机 RAM OOM。
- `output_format` 只开 `video/h264-mp4`(默认)/`video/h265-mp4`/`video/webm`:三者的格式
  专属参数(pix_fmt=yuv420p、crf、save_metadata)与快照兼容,产物扩展名都归 video。
  gif/webp 会被桥归成 image,ProRes/nvenc 需要别的参数,故不开放。

## 部署

**先上线后端代码**,再跑
`BASE=http://127.0.0.1:8000 ADMIN_TOKEN=… ./docs/replications/upscale/deploy.sh`:
POST 模板(校验返回的 `output_kind`)→ PUT mapping(失败就删掉刚建的模板)→ 调
`fix-image-outputs.sh`,把修复前建的图片模板(`nous-krea2`、`nous-qwen21-text-to-image`、
`nous-qwen21-image-edit`)的 exposed_outputs 从 `video_url` 改成 `image_url`(按名字查 id;
已改过的跳过;形状不认识的拒改)。可重跑,**也是改参数的唯一入口**:模板已存在时把仓库里的
workflow / mapping 同步上去(只写有差异的部分,比较前忽略后端补的 null/false 字段),所以改了
`*.mapping.json` 的默认值或范围后直接重跑即可,不用手动 `PUT …/mapping`。输出类型只在新建时定。
不碰 DB、不重启。卸载:`DELETE /api/v1/comfy-templates/{id}`。
