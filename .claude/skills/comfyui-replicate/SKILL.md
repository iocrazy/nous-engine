---
name: comfyui-replicate
description: 复刻 ComfyUI 工作流到 nous-center 全流程 SOP:解析 JSON → 模型/节点能力盘点 → 缺口走 PR → API 搭画布工作流 → 真机连通性验证(含 LoRA 生效量化判据)→ 发布外部服务 → 生成模块化卸载 manifest。用户给出 ComfyUI 工作流 .json(或说「复刻这个工作流」)时使用。
---

# 复刻 ComfyUI 工作流到 nous-center

输入:ComfyUI 工作流 `.json` 路径(UI 导出格式,含 `nodes/links/groups`)。
产出:画布工作流(可选 A/B 调试版)+ 已发布服务 + API key + **卸载 manifest**。

每个阶段做完再进下一个;真机验证不过不许发布。

## 阶段 1:解析工作流 JSON

用 python 解析,提取四类信息:

1. **模型资产**:UNETLoader / CheckpointLoader / CLIPLoader / VAELoader / LoraLoader* 的
   widgets_values 里的文件名。
2. **采样参数**:KSampler*/SamplerCustomAdvanced 链上的 steps、cfg、sampler、scheduler、
   seed、denoise。注意 ComfyUI 把参数拆在多个节点(KSamplerSelect=采样器,
   Flux2Scheduler=步数+分辨率 shift,CFGGuider=cfg,RandomNoise=种子)——
   **widget 顺序要对照 ComfyUI 源码确认**(`/home/heygo/sites/ComfyUI/comfy_extras/`,
   如 `Flux2Scheduler(steps, width, height)`),别凭 widgets_values 位置猜。
3. **图像流**:LoadImage → 缩放 → VAEEncode → ReferenceLatent 链(多参考图)/
   LatentImage 尺寸来源(GetImageSize?)。ReferenceLatent 串 N 个 = N 张参考图,
   正负 conditioning 各一链是 ComfyUI 模板写法,nous 不需要复刻
   (diffusers pipeline 内部处理,`image_ref_join` 逗号串等价,顺序同 = 先挂的是图1)。
4. **提示词**:CR Text / CLIPTextEncode。注意 CLIPTextEncode 框里显示的 widget 值
   可能是历史残留,**有 STRING 输入连线时以连线为准**;没接线的 CR Text 是备用提示词。

## 阶段 2:资产与能力盘点

- 模型文件对照 `LOCAL_MODELS_PATH`(backend/.env,通常 `/media/heygo/Program/models/nous`)
  的 `image/{diffusion_models,text_encoders,vae,loras}/`。**缺的列清单给用户下载,别继续装作能跑**。
- nous 节点能力对照 `backend/nodes/*/node.yaml` + `src/services/nodes/`。
  常用等价映射:

  | ComfyUI | nous |
  |---|---|
  | UNETLoader | flux2_load_diffusion_model |
  | CLIPLoader | flux2_load_clip(clips 数组) |
  | VAELoader | flux2_load_vae |
  | LoraLoaderModelOnly | flux2_load_lora(lora_name **带扩展名** + lora_path 绝对路径 + strength) |
  | CLIPTextEncode | flux2_encode_prompt(text 从 text_input 接) |
  | KSamplerSelect+Scheduler+CFGGuider+RandomNoise+EmptyLatent | flux2_ksampler 一个节点 |
  | SamplerCustomAdvanced+VAEDecode | flux2_vae_decode(dispatch 终端) |
  | LoadImage | image_input(上传 base64 data URI) |
  | ReferenceLatent ×N | image_ref_join(可串联,A 口=图1) |
  | Image Comparer (rgthree) | image_compare |
  | PreviewImage/SaveImage | image_output |

- **节点缺口走独立 PR**(分支+worktree+测试+CI 绿+合并,见 memory feedback 系列)。
  教训:细粒度画布路径很多「legacy 验过」的能力其实没人走过(#484 LoRA 静默 no-op、
  #485 adapter 名含点崩溃都是这么逮到的)——别信「测试有」,要真机出图验。

## 阶段 3:API 搭建工作流

- 登录:`POST /sys/admin/login {password}`(backend/.env 的 ADMIN_PASSWORD)拿 cookie。
  坑:本机代理要 `--noproxy '*'` / urllib 装空 ProxyHandler;curl cookie jar 的
  `#HttpOnly_` 行不是注释;cookie 值要 strip 换行。
- `POST /api/v1/workflows {name, description, nodes, edges}`。节点形态
  `{id, type, data, position, style}`,边 `{id, source, sourceHandle, target, targetHandle}`。
- **关键参数规则**:
  - KSampler 宽高 = **主图(图1)比例**(对齐 ComfyUI GetImageSize 语义)。
    设成与参考图同比例会把主体锚到参考图(2026-06-11 实锤)。
  - A/B 对比:两条 lane 共享 loaders/encode/join,只差 LoRA 节点;seed 填同值即等价
    ComfyUI 共享 RandomNoise。
  - device 用 cuda:1(Pro 6000 96G);CUDA 索引坑见 memory user_hardware。

## 阶段 4:连通性真机验证(必过门禁)

1. 冒烟(LoRA 置空=ComfyUI 禁用语义):全节点跑通、出图、参考图逗号串段数对。
2. 真参数跑:**LoRA 生效的判据是像素 diff**——同种子 LoRA/基模两图
   `np.abs(a-b).mean()` 必须显著非零。bit 相同 = LoRA 没生效,哪怕日志显示
   combo hash 不同、adapter 重建了(#484 的坑:缓存键变了≠权重应用了)。
3. 语义验证:主体保持(主体来自图1)、迁移方向对;真实素材跑一张眼看。
4. 日志:dev 后端在 `backend/logs/backend-dev.log`(dev-serve.sh);
   `image adapter MISS/HIT` 行的 `loras: [...]` 是组件级证据,但**不是**权重应用证据。

## 阶段 5:发布服务

- 服务版 = **单链**(只留生效 lane);A/B 版留画布调试。
- `POST /api/v1/workflows/{id}/publish`:
  - `name` 必须 `^[a-z][a-z0-9-]{1,62}$`
  - `exposed_inputs`:`{node_id, key, input_name, label, type, required, default}`;
    image_input 的 input_name 用 `image`,text_input 用 `text`,
    ksampler 可暴露 width/height/seed,lora 节点可暴露 strength。
  - `exposed_outputs` 只能指向 `flux2_vae_decode` 节点,字段用 `image_url`。
- key:`POST /api/v1/keys {label, service_ids:[svc.id]}` → 返回 secret(只此一次)。
- 验证:`POST /v1/apps/{name}` + `Authorization: Bearer <secret>`,body 用 exposed key,
  取 `outputs.<dec节点id>.image_url` 下载眼看。
- 公网:nous-engine 不对外暴露(公网隧道 2026-09-06 退役),不要给它装 cloudflared。

## 阶段 6:卸载 manifest(模块化标注)

每次复刻在 `docs/replications/<服务名>.md` 写一份 manifest 并随 PR 提交,格式:

```markdown
# <服务名> 复刻 manifest
来源: <ComfyUI json 文件名> | 日期: YYYY-MM-DD
## 创建的资源(卸载按从下往上顺序删)
- API key: <key_id / label>          → DELETE /api/v1/keys/{id}
- 服务: <service_id / name>          → DELETE /api/v1/services/{name}(或置 retired)
- 服务版工作流: <workflow_id>        → DELETE /api/v1/workflows/{id}
- 调试版工作流: <workflow_id>        → DELETE /api/v1/workflows/{id}
- 模型文件: <路径列表>(共享资产,确认无他人引用再删)
- 专为本复刻合的 PR: #xxx(代码级能力,通用,一般不回滚)
## exposed 契约
<入参/出参表,方便外部对接>
```

memory 里同步一行索引(project_<name>_replication)。

## 完成定义

缺一不可:真机出图验证通过、外部 Bearer 调用通过、manifest 落盘、memory 更新。
