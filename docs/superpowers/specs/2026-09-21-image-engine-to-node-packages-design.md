# 删除自建图像引擎,代码以 git tag 存档 — 设计

**日期**:2026-09-21 起草(插件化方案);**2026-09-26 改写为「删 + tag」**
**状态**:边界待用户确认(见 §8 唯一决策点),确认后进 writing-plans
**动机**:ComfyUI 桥上线后,自建原生图像路径成为冗余。原方案是「插件化保留」;
2026-09-26 用户裁定改为**直接删除,代码靠 git tag 留档**(原话:「同意把 spec 从『插件化』
改成『删 + tag』,studio-upscale 直接删,我后续可以换桥服务」)。

改写理由:插件化要新开 adapter-provider 注册表、搬 2.4MB 的 `seedvr2_vendor/`、改 CI 收集
路径,三个阶段的纯重构只为保住一份**生产环境根本装不起来**的代码(见 §1.0)。git 本身就是
存档,tag 一打,哪天要回来 `git checkout image-engine-native-final -- backend/src/...` 即可。

---

## 1. 研判:能不能删

结论:**能,而且生产上它们早就是死的。** 证据全部在真机/代码上查实。

### 1.0 生产 venv 没装 diffusers(最硬的一条)

CLAUDE.md 明文:生产 `.venv` 只装 `--extra inference`,**没有 diffusers**(在 `image` extra)。
所有原生图像引擎(`image_modular.py` 等)第一行 `from diffusers import …` 就 ImportError。
真机佐证:`studio-upscale` 是 8 个服务里**唯一**有授权的(`nous-app (llm+embedding)` key,
2026-09-24 授的),两次运行**全部 failed**。其余 7 个服务 0 授权 0 运行。

### 1.1 对外契约零损失

`/v1/images/generations`(`openai_compat.py`)**不按 `category` / `source_type` 过滤**,只看
`ApiKeyGrant` + `ServiceInstance.name` + `workflow_snapshot`。桥服务(`krea2` /
`minimax-h3-dialogue` / `minimax-h3-r2v`,`source_type=comfy_template`)三条全满足 —— 删掉自建
不丢任何对外 API。

### 1.2 两条路共用同一执行核心

都经 `WorkflowExecutor` + `workflow_service_runner.run_published_workflow`。差别只在工作流里放
哪个节点:自建放 `flux2-components` 的 dispatch 节点(→ runner 子进程 →
`get_or_load_image_adapter`),桥放 `comfy_bridge` 节点(→ HTTP 到 ComfyUI)。**删的是一组节点
实现和它的 runner 后端,不是架构。**

### 1.3 能力逐项可替代

| 自建服务 | ComfyUI 侧 |
|---|---|
| `studio-upscale`(SeedVR2 超分) | `ComfyUI-SeedVR2_VideoUpscaler` 自定义节点已装;权重在 `models/nous/media/SEEDVR2/`。**用户已表态:删了之后需要时换桥服务** |
| `studio-text-to-image` / `studio-image-edit` / `studio-angle` | Z-Image / Flux2 / Qwen-Image-Edit 权重均在 ComfyUI 模型树 |
| `ideogram4` / `wanwu-qianyi` / `img-flux2` / `img-wikeeyang` | 同上,且桥另有 275 风格与自定义节点生态 |

**权重从来只有一份**:自建路径读的就是 `models/nous/media/`,即 ComfyUI 的模型目录。删代码不删权重。

### 1.4 维护税

`diffusers` 钉死 git commit(`pyproject.toml` `image` extra),注释自己写明 Modular Diffusers 是
**EXPERIMENTAL,API 跨 commit 就破**;每次 bump 要跑 `tests/manual/smoke_image_ab.py` 的 SSIM
golden 回归。它还牵着 ~470 个单元测试用例和 ~60 个 manual smoke,CI 每次都在验证一份生产装
不起来的代码。

### 1.5 额外收益

- CLAUDE.md「数据面对放置只读」不变式的**例外之一消失**(「图像路径经
  `get_or_load_image_adapter`,仍会在执行期按需加载模型」)。
- 后端启动少 fork 一个 `image` runner 子进程(`hardware.yaml` 的 `image` 组);少一套
  component-state WS 广播。

---

## 2. 删除边界(四层 + 卫星)

原 spec 只数了 4 层;2026-09-26 按 `import` 反向依赖重新盘,**卫星模块远多于当初估计**。
判据只有一条:**删掉引擎后没有任何非图像调用方的模块,一起删**。留下来就是打到 500 的死按钮。

### 2.1 删:引擎实现(`backend/src/services/inference/`)

| 文件 | 行数 | 说明 |
|---|---|---|
| `image_modular.py` | 1978 | Modular Diffusers 引擎,`diffusers` 的唯一 import 集中点 |
| `image_seedvr2.py` | 500 | SeedVR2 超分 |
| `image_anima.py` + `arch_anima/` | 203 + 100K | Anima 自定义 DiT(arch registry 的一个分支) |
| `seedvr2_compat.py` + `seedvr2_vendor/` | 42 + 2.4MB | 内嵌上游 NumZ 源码 |
| `lcs_integration.py` + `lcs_vendor/` | 255 + vendor | LCS 采样期锐化干预,只挂在自建 KSampler 的 post-CFG hook 上 |
| `model_arch_adapter.py` | 267 | diffusers Pipeline 家族差异抽象 + arch registry |
| `quant_loaders.py` | 425 | fp8/GGUF 单文件反量化(`diffusers.loaders.single_file_utils`) |
| `sigma_schedules.py` | 241 | ComfyUI 调度器 sigma 移植,只喂自建采样 |
| `pinned_stash.py` | 160 | RAM pinned stash 搬运,只服务 diffusers 组件卸载/回载 |
| `image_l2_cache.py` | 89 | runner 侧出图 L2 缓存。**原 spec 说「桥的出图同样受益」是错的**:唯一调用方是 `runner_process.py:522-543/753`,在图像节点分支内;桥节点走主进程 HTTP,不经 runner |

### 2.2 删:卫星子系统(`backend/src/services/`、`src/api/`、`src/runner/`)

| 模块 | 消费者(删后归零) | 说明 |
|---|---|---|
| `component_scanner.py` / `component_state.py` / `inference/component_spec.py` | `main.py` 启动扫描 + selfcheck、`routes/components.py`、`engine_catalog.py`、`model_deleter.py`、`ws_hub.broadcast_component_state` | 「组件库」= diffusion_models / clip / vae / loras / checkpoint 单文件索引,只给 flux2 组件装配用 |
| `lora_scanner.py` + `routes/loras.py` | `engines.py`(`count_loras_for_arches`)、`model_manager.get_lora_paths`、`model_deleter.py`、前端 `lora_stack` widget | LoRA 只有 flux2 comfy 格式转换一条用途(`test_flux2_comfy_lora_convert`);vLLM 路径不用 LoRA |
| `latent_storage.py` | `runner_process.py:723`(路 B latent 接力) | 只有 flux2 分段采样用 |
| `routes/components.py` | 前端 `api/components.ts` | `/api/v1/components`、`/seedvr2-dit`、`/scan` |
| `engines.py` 8 个端点 | 前端 `api/engines.ts` | `/component/{preload,unload,resident}`、`/seedvr2/{preload,unload,resident}`、`/image-cache`;**`/loaded-adapters` 留**(TTS runner 也经 `runner_models.py` 上报) |
| `runner_process.py` 图像分支 | — | `PreloadComponents` / `PreloadSeedVR2` 处理器、`seedvr2_upscale` / `flux2_vae_decode` 终端派发、image_l2、latent 落盘、`pinned_ram_mb` 快照字段;**runner 框架与 TTS 派发原样留** |
| `runner/protocol.py` 消息 | — | `PreloadComponents`、`PreloadSeedVR2`、`ComponentEvent`;`RunnerClient.on_component_event` |
| `engine_catalog.py` 三种 kind | 引擎库(模型页) | `kind=upscale`(SeedVR2 DiT 白名单)、`component`、`lora` 条目生成器;引擎库只剩 llm / asr / embedding / ocr / tts |
| `workers/image_worker.py` + `routes/generate.py`(image 项)+ `celery_app.py` 队列 | 前端**无**调用(grep 零命中) | Celery 图像任务,legacy。plan 里核实 `understand` 路由是否同属 legacy,同规则处理 |
| `main.py` | — | 组件索引启动扫描(:558-567)、`_make_component_event_handler`(:31-43)、component-state mirror(:670)、`image` runner 组 fork |
| `monitor.py` | 前端监控页 | `pinned_ram_mb` 字段(来源 `pinned_stash.total_pinned_bytes`) |

### 2.3 删:节点包与配置

- `backend/nodes/flux2-components/`、`backend/nodes/seedvr2/`、`backend/nodes/lcs/`(LCS 只产
  给自建 KSampler 的干预描述符,桥不认)
- `pyproject.toml` 整个 `image` extra(含钉 commit 的 `diffusers`)。`color-matcher` 在 base
  dependencies(:31),不受影响 —— 已查实
- `configs/hardware.yaml` 的 `image` 组(role image)。`_placement` 按 role 查组,删组后没有节点
  声明 role image,不会有人查。**`llm` / `tts` 两组不动**
- `models.d/` 没有 `type: image` 的 yaml —— 已查实,无需动

### 2.4 删:数据

- `service_instances` 8 行(`source_type=workflow, category=image`):`ideogram4`、`img-flux2`、
  `img-wikeeyang`、`studio-angle`、`studio-image-edit`、`studio-text-to-image`、`studio-upscale`、
  `wanwu-qianyi`;连带各自 `auto_generated` 的 `workflows` 行与 `studio-upscale` 的 1 条
  `api_key_grants`
- 其它引用原生图像节点类型(`flux2_*` / `seedvr2_*` / `lcs_*` / `anima*`)的**画布工作流**
  (含未发布草稿):plan 第一步经 `GET /api/v1/workflows` 盘点,列给用户,**逐条确认后删**。
  不删的话打开会渲染成未知节点、执行报 `ExecutionError("未知节点类型")`(现有行为,
  `workflow_executor.py:590`,不新加接缝)

### 2.5 删:测试与 manual

- 单元测试整文件删(~45 文件 / ~470 用例):`test_anima_*`、`test_arch_anima_*`、`test_flux2_*`、
  `test_seedvr2_*`、`test_component_*`、`test_components_*`、`test_image_{adapter_card_guard,
  arch_registry,engine_selector,lora_cleanup,model_integration,modular_wiring,sampler_ssim}`、
  `test_get_or_load_image_adapter`、`test_ideogram4_*`、`test_lora_scanner`、`test_loras_route_cache`、
  `test_lowvram_auto_stream`、`test_model_arch_adapter`、`test_pinned_stash_unpin`、
  `test_adapter_ram_stash`、`test_quant_*`、`test_sigma_schedules`、`test_split_sigmas`、
  `test_latent_storage`、`test_runner_components_dispatch`、`test_runner_ipc_ideogram4`、
  `test_unified_mgmt_combo_arch_pr2`、`test_inference_request_components`、
  `test_granular_workflow_dispatch`、`test_runner_build_request_granular`、`test_image_l2_cache`、
  `test_runner_l2_cache`
- 单元测试**部分删用例**(~20 文件,按跑红逐个处理,只删图像相关 case,不放宽断言):
  `test_api_engines`、`test_engine_catalog`、`test_runner_process*`、`test_runner_protocol`、
  `test_runner_client`、`test_runner_supervisor`、`test_runner_models_aggregate`、`test_api_monitor`、
  `test_health_endpoint`、`test_model_delete`、`test_model_scanner`、`test_config`、`test_workers`、
  `test_celery_app`、`test_startup_stack_check`、`test_service_models`、`test_service_autostart`、
  `test_node_routing`、`test_workflow_executor_*`、`test_gpu_group_tp`、`tests/fixtures/fake_runner.py`、
  `tests/chaos/test_worker_crash_storm.py`、`tests/integration/test_runner_resilience.py`
- `tests/manual/` 约 60 个 `smoke_*` / `spike_*` / `probe_*` / `verify_ideogram4_*`(zimage、
  flux2、anima、ideogram4、seedvr2、lcs、latent、schedulers、quant、offload 全系)+ golden 图。
  **`verify_wemm_embedding.py` 与 TTS/ASR 的 smoke 不动**
- CI `--ignore` 列表不变(两个被 ignore 的文件与图像无关)

### 2.6 删:前端(10 文件,`frontend/src/`)

| 文件 | 处置 |
|---|---|
| `api/components.ts`、`api/loras.ts` | 整删(含 `.test.tsx`) |
| `api/engines.ts` | 删 `usePreloadSeedvr2` / `useUnloadSeedvr2` / `useSetSeedvr2Resident` / `usePreloadComponent` / `useSetComponentResident` / `useUnloadComponent` / `useUnloadImageAdapters`;**`useLoadedAdapters` 留**(TTS) |
| `models/nodeRegistry.ts` | 删 widget 类型 `component_select` / `lora_stack` / `lora_select` / `clip_stack` / `seedvr2_model_select` |
| `models/workflow.ts`、`components/nodes/portColors.ts` | 删端口类型 `seedvr2_dit` / `seedvr2_vae` / `seedvr2_compile` |
| `components/nodes/DeclarativeNode.tsx` | 删上述 widget 的渲染分支(`Seedvr2ModelSelectWidget` 等)与 components / loras 的 hook import |
| `components/workflow/appEditorSchema.ts` | 删 `seedvr2_model_select` 分支 |
| `pages/studioWorkflows.ts` | 8 个 studio 预设工作流的模板;plan 里核实有没有非图像预设,**没有就整删** |
| `components/overlays/ModelsOverlay.tsx` | 删 SeedVR2 / 组件 预热·常驻·卸载 区块 |
| `components/overlays/DashboardOverlay.tsx` | 删 `useUnloadImageAdapters` 入口;`useLoadedAdapters` 留 |

### 2.7 必须留(删了会搞挂桥或别的引擎)

- `image_output_storage.py` —— `nodes/comfy_bridge.py:36` 直接 `from ... import write_image`
- `routes/image_files.py` —— 出图分发
- `src/services/nodes/image.py`(`image_output` sink,透传 `image_url`)
- `nodes/image-io`、`nodes/image-color-match` —— 纯 CPU inline 节点,只认 `image_url`
- `runner_process.py` 框架 + TTS 派发;`runner_models.py` + `/api/v1/engines/loaded-adapters`
- `workflow_service_runner.py` / `predictions.py` / `WorkflowExecutor` / `nodes/` 框架 / `comfy_bridge.py`
- `/v1/images/generations` 端点
- `models/nous/media/` 全部权重(ComfyUI 的树)
- `main.py` 的 `image_orphan_reap_loop`(:328-350,清 `image_output_storage` 孤儿,桥的出图也靠它)
- 历史 spec 文档(`docs/superpowers/specs/2026-05-22-image-engine-modular-diffusers-design.md` 等)
  —— 文档不删,是 tag 之外的第二份索引

---

## 3. 存档方式

1. 删除 PR 合并**前**,在 `master` 打 annotated tag:
   ```
   git tag -a image-engine-native-final -m "自建图像引擎(Modular Diffusers / SeedVR2 / Anima / LCS)删除前的最后一版;
   见 docs/superpowers/specs/2026-09-21-image-engine-to-node-packages-design.md"
   git push origin image-engine-native-final
   ```
   tag 打在删除 PR 的 base commit 上,不是 PR 分支上。
2. 本 spec 保留在 `docs/superpowers/specs/`,§2 就是「当年删了什么」的清单;CLAUDE.md
   「图像引擎 (image engine)」一节整段替换成三行:已删、tag 名、找回命令。
3. 不做别的存档(不拷进 `docs/archive/`、不单开仓库)。

---

## 4. 落地顺序与回滚点

三步,每步结束都是可上线、可回滚的状态。**第 1 步是运行时操作不是代码**,由主会话经 API
执行(与「模型放置只能由控制面改」同一原则:数据删除走控制面 API,不手写 SQL)。

1. **盘点 + 数据拔出**(生产运行时,先于任何代码)
   - `GET /api/v1/workflows` 找出所有引用原生图像节点类型的画布工作流(含草稿),连同 8 个
     服务的清单一起给用户过目;
   - 用户确认后:`DELETE /api/v1/services/{id}` × 8(核实级联:自动生成的 workflow 行与
     `studio-upscale` 的 grant 是否随删;不随删就分别 DELETE),再删确认的画布工作流;
   - `invalidate("services")` 由端点自带;`/v1/models?include_unready=1` 上应看不到这 8 个。
   **回滚点 A**:代码零变化;服务可从 `workflow_snapshot` 重新发布(存档在 tag 里的
   `studioWorkflows.ts` 模板)。
2. **打 tag → 删除 PR**(一个分支,后端 + 前端两组 commit,**一次部署**)
   后端删 `/api/v1/components` 等端点会让旧前端的组件页打 404,所以后端与前端必须同一次
   `deploy.sh` 上线,不拆 PR。顺序:
   1. 删 §2.1–2.3 的代码与配置;`uv run ruff check .` 过;
   2. 删 §2.5 整文件测试;跑全量,按红的逐个处理「部分删用例」文件;
   3. 删 §2.6 前端;`npm run lint && npx tsc -b && npx vitest run`;
   4. 改 CLAUDE.md(§3 第 2 条)、删 `.superpowers` 里图像相关 SDD 工作区(若有)。
   **回滚点 B**:`git revert` 整个 merge commit,或 `git checkout image-engine-native-final -- <路径>`。
3. **上线后核验**
   `enginectl status` 三个单元 active;`/health` `status=ok`;`journalctl` 无 ImportError;
   `/api/v1/engines` 引擎库不再出现 upscale / component / lora 条目;`nvidia-smi` 上 GPU 0 只剩
   常驻三样(本来就没图像模型,预期无变化);`krea2` 桥服务 `ready=true` 且能出图
   (`POST /v1/services/krea2/predictions`)。

生产 venv **不用动**:`image` extra 从来没装过,`uv sync --extra inference` 结果不变。
**绝不裸 `uv sync`**(会裁掉 vllm/torch)。

---

## 5. 测试策略

- 没有新行为,所以**没有新测试**。唯一「新」的可观察行为是「引用已删节点类型的工作流执行报
  `未知节点类型`」,这是现有 `workflow_executor.py:590` 的既有路径,已有覆盖
  (`test_workflow_executor_dispatch`),不重复加。
- 门禁是**减法后全绿**:`uv run pytest tests -n 8`(基线 2334 passed,删后预期 ≈1860)+
  `ruff` + 前端三件套。任何「为了让测试过而放宽断言」的改动都是红线 —— 只允许删用例,
  不允许改断言。
- `tests/test_data_plane_readonly.py` 与 `tests/test_resident_capacity.py` 不受影响
  (前者锁的五个路由模块不含图像;后者只看 `hardware.yaml` 的 llm 组容量)。

---

## 6. 已知风险

1. **画布工作流里的草稿**引用了原生节点而没被盘点到 —— 打开会是一排未知节点。缓解:第 1 步
   按节点类型正则全量扫 `workflows.nodes`,不靠人记。
2. **`runner_process.py` 拆分**是本次唯一有「改而非删」的地方(TTS 派发与图像派发在同一个
   函数里)。缓解:先跑 `test_tts_node_is_dispatch` / `test_runner_process*` 锁住 TTS 行为再动;
   拆完两卡上 `qwen3-tts` 真跑一段音频(manual)。
3. **`engine_catalog.py` 的 `type_filter`** 逻辑(:178)按 `image` 分支;删 kind 时别把 `tts`
   的过滤一起删了。
4. **`model_scanner.py` / `model_metadata_service.py`** 提到 diffusers 的只有注释(已 grep,
   无真 import),但可能有 `media/` 桶的 depth 特例 —— plan 里核实,若只为图像服务就删。
5. `routes/generate.py` / `understand.py` 的 Celery 路径是否还有别的消费者 —— 前端 grep 零命中,
   但 plan 里再核 `celery_app` 是否被 systemd 起(没有 `nous-engine-celery` 单元 → 死代码)。

---

## 7. 明确不做

- 不删 `models/nous/media/` 任何权重(ComfyUI 共用)
- 不动画布工作流子系统本身(`nodes/` 框架、`llm.py`、`audio.py`、`comfy_bridge.py`、TTS 节点包)
- 不动 `/v1/images/generations` 端点
- 不给 ComfyUI 加超分桥服务(用户说「后续可以换桥服务」,那是另一件事,由用户发起)
- 不改 Pro 6000 的显存策略(另案,待用户决策)
- 不新开插件接缝、不改 CI 收集路径(原插件化方案的两项工作随方案作废)

---

## 8. 唯一待确认的决策点

§2.2 把**组件库(`/api/v1/components`)、LoRA 库(`/api/v1/loras`)、RAM pinned stash、latent 接力**
一并划进删除边界。理由是删掉引擎后它们没有任何调用方,留着就是模型页上一排打到 500 的按钮。
若用户希望保留组件库/LoRA 库的**浏览**功能(只看不预热),边界收回到 §2.1 + runner 分支 +
节点包,§2.2 的 scanner 两项改为「只删 preload/resident 端点,保留 GET 列表」。
**默认按全删执行。**
