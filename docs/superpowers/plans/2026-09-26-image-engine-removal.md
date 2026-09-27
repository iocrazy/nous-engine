# 删除自建图像引擎(删 + git tag 存档)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把自建原生图像引擎(Modular Diffusers / SeedVR2 / Anima / LCS)及其全部卫星子系统从
nous-engine 删干净,代码只靠 git tag `image-engine-native-final` 留档;桥(ComfyUI)、TTS、LLM
路径零变化。

**Architecture:** 纯减法,**没有新行为、没有新测试**。按「由外向内」顺序删:先前端、再后端
API 层、再 runner 层、再 ModelManager,最后物理删模块/节点包/依赖 —— 每一步删的都是**消费者**
先于**被消费者**,所以每个 task 结束时 import 图是闭合的、测试全绿、可独立上线(但整支
后端+前端必须一次部署,见 Task 7)。

**Tech Stack:** Python 3.13 / FastAPI / pytest-xdist(PostgreSQL 临时库);React + TypeScript /
vitest;uv;git worktree。

**Spec:** `docs/superpowers/specs/2026-09-21-image-engine-to-node-packages-design.md`(2026-09-26
「删 + tag」改写版)。§2 是删除清单,§2.7 是**必须留**清单,§4 是落地顺序。任何「删不删」的
疑问以 spec §2 / §2.7 为准;两边都没写的,按判据「删掉引擎后有没有非图像调用方」定。

## Global Constraints

- **工作目录是 worktree** `/media/heygo/program/projects-code/repos/nous-engine-image-removal`,
  分支 `spec/image-engine-removal`。**主检出 `../nous-engine` 是生产,绝不在那里改代码、绝不
  `npm run build`(会覆盖生产 dist)、绝不重启任何 `nous-engine-*` 单元、绝不写生产 API。**
- 后端测试**复用生产 venv 但绝不改它**:每个 shell 先
  ```bash
  export UV_PROJECT_ENVIRONMENT=/media/heygo/program/projects-code/repos/nous-engine/backend/.venv
  export UV_NO_SYNC=1
  export DATABASE_URL="$(grep '^DATABASE_URL=' /media/heygo/program/projects-code/repos/nous-engine/backend/.env | cut -d= -f2- | tr -d '"')"
  ```
  **绝不裸 `uv sync`**(会裁掉 vllm/torch 搞挂线上);`UV_NO_SYNC=1` 就是为了让 `uv run`
  永不碰 venv。删了 `image` extra 后 `uv lock` 只改 `uv.lock` 文件,不装不卸任何东西。
- 测试绝不能真起推理服务(conftest 有 Popen 护栏);跑全量前 `nvidia-smi` 确认驱动活着;
  全量用 `uv run pytest tests -n 8 -q`,**别与其他 agent 同时跑全量**。
- **只准删用例,不准改断言**让测试通过。一个文件里非图像用例全删光了就整删文件。
- 前端在 worktree 里:`cd frontend && npm ci`(首次);验证用
  `npm run lint && npx tsc -b && npx vitest run && npx vite build`(**不要 `npm run build`**,
  它的 prebuild 要 wasm-pack,worktree 没装;`npx vite build` 只写 worktree 自己的 dist)。
- 每个 task 一个 commit,中文 message,写清「删了什么 + 为什么」;不 push、不开 PR(Task 7 主会话做)。
- 必须留的东西(spec §2.7),逐字:`image_output_storage.py`、`routes/image_files.py`、
  `services/nodes/image.py`、`nodes/image-io`、`nodes/image-color-match`、runner 框架 + TTS 派发、
  `runner_models.py` + `GET /api/v1/engines/loaded-adapters` + `POST /loaded-adapter/unload`、
  `workflow_service_runner.py` / `predictions.py` / `WorkflowExecutor` / `nodes/` 框架 /
  `comfy_bridge.py`、`/v1/images/generations`、`models/nous/media/` 权重、`main.py` 的
  `image_orphan_reap_loop`、`ModelManager.stash_model` / `stash_ram_bytes` /
  `_stash_ram_reserve_bytes`(adapter 级 RAM stash)、`hardware.yaml` 的 `llm` / `tts` 组、历史 spec 文档。

---

### Task 0: 盘点 + 拔数据(生产运行时;**主会话执行,不派 subagent**;每一步先给用户看再做)

**Files:** 无代码改动。全部经控制面 API(`ADMIN_TOKEN` 在 `../nous-engine/backend/.env`)。

**Interfaces:**
- Produces: 8 个图像服务的 id 列表、引用原生节点的画布工作流 id 列表 —— 都已删除,后续 task
  不依赖它们,但 Task 7 上线核验要确认 `/v1/models?include_unready=1` 不再有它们。

- [ ] **Step 1: 列出 8 个图像服务及其 id**

```bash
TOKEN=$(grep '^ADMIN_TOKEN=' ../nous-engine/backend/.env | cut -d= -f2- | tr -d '"')
curl -s -H "Authorization: Bearer $TOKEN" 127.0.0.1:8000/api/v1/services | python3 -c '
import json,sys
for s in json.load(sys.stdin):
    if s.get("category")=="image" and s.get("source_type")=="workflow":
        print(s["id"], s["name"], s.get("status"))'
```
Expected:8 行 —— `ideogram4` `img-flux2` `img-wikeeyang` `studio-angle` `studio-image-edit`
`studio-text-to-image` `studio-upscale` `wanwu-qianyi`。**多一个少一个都停下问用户。**

- [ ] **Step 2: 列出引用原生图像节点类型的画布工作流(含草稿)**

```bash
curl -s -H "Authorization: Bearer $TOKEN" 127.0.0.1:8000/api/v1/workflows | python3 -c '
import json,re,sys
pat=re.compile(r"^(flux2_|seedvr2_|lcs_|anima|z_image|qwen_edit|ideogram)")
for w in json.load(sys.stdin):
    types={n.get("type","") for n in (w.get("nodes") or [])}
    hit=sorted(t for t in types if pat.match(t))
    if hit: print(w["id"], w.get("name"), w.get("status"), w.get("auto_generated"), w.get("generated_for_service_id"), ",".join(hit))'
```
把清单原样贴给用户,**逐条确认要删哪些**(auto_generated 且 generated_for_service_id 指向
8 个服务之一的,随服务删;其余是用户手画的草稿,由用户定)。

- [ ] **Step 3: 删 8 个服务(用户确认后)**

```bash
for id in <8 个 id>; do
  curl -s -o /dev/null -w "$id %{http_code}\n" -X DELETE -H "Authorization: Bearer $TOKEN" 127.0.0.1:8000/api/v1/services/$id
done
```
Expected:每行 `204` 或 `200`。然后核实级联:
```bash
curl -s -H "Authorization: Bearer $TOKEN" 127.0.0.1:8000/api/v1/keys | python3 -c '
import json,sys; [print(k["id"], k.get("label"), [g.get("service_name") or g.get("service_id") for g in k.get("grants",[])]) for k in json.load(sys.stdin)]'
```
`studio-upscale` 的 grant 若还在(字段结构以实际响应为准),`DELETE /api/v1/keys/{key_id}/grants/{grant_id}`。
再跑 Step 2 的脚本:auto_generated 的工作流应已随服务消失;没消失的用
`DELETE /api/v1/workflows/{id}` 逐个删。

- [ ] **Step 4: 删用户确认的画布草稿**(同上 `DELETE /api/v1/workflows/{id}`)

- [ ] **Step 5: 核验**

```bash
curl -s -H "Authorization: Bearer $TOKEN" "127.0.0.1:8000/v1/models?include_unready=1" | python3 -c 'import json,sys; print(sorted(m["id"] for m in json.load(sys.stdin)["data"]))'
```
Expected:没有任何 `studio-*` / `img-*` / `ideogram4` / `wanwu-qianyi`。
无 commit(不是代码)。在 SDD ledger 记一行 `Task 0: complete — 删了 N 个服务、M 个工作流`。

---

### Task 1: 前端删除(创作台、组件/LoRA/SeedVR2 全部 UI 与 API 封装)

**Files:**
- Delete: `frontend/src/pages/Studio.tsx`、`frontend/src/pages/studioWorkflows.ts`、
  `frontend/src/pages/studioWorkflows.test.ts`、`frontend/src/api/components.ts`、
  `frontend/src/api/components.test.tsx`、`frontend/src/api/loras.ts`、
  `frontend/src/components/nodes/ComponentSelectWidget.test.tsx`、
  `frontend/src/components/nodes/ComponentStatusHeader.test.tsx`、
  `frontend/src/components/nodes/ClipStackWidget.test.tsx`,以及这三个测试对应的组件文件
  (`grep -rln "ComponentSelectWidget\|ComponentStatusHeader\|ClipStackWidget" frontend/src` 找到的非测试文件)
- Modify: `frontend/src/App.tsx:146`(`/studio` 路由)、`frontend/src/components/layout/IconRail.tsx:46,58`
  (`studio` 路径与「创作台」项)、`frontend/src/api/engines.ts:150-270,389-433`
  (`usePreloadSeedvr2` `useUnloadSeedvr2` `useSetSeedvr2Resident` `usePreloadComponent`
  `useUnloadComponent` `useSetComponentResident` `useUnloadImageAdapters`;**`useLoadedAdapters`
  与 `useUnloadAdapter` 留**)、`frontend/src/models/nodeRegistry.ts:3`(WidgetType 去掉
  `component_select` `lora_stack` `lora_select` `clip_stack` `seedvr2_model_select`)、
  `frontend/src/models/workflow.ts:3` 与 `frontend/src/components/nodes/portColors.ts:23-26`
  (去掉 `seedvr2_dit` `seedvr2_vae` `seedvr2_compile`)、
  `frontend/src/components/nodes/DeclarativeNode.tsx`(删上述 widget 的 `case` 分支、
  `Seedvr2ModelSelectWidget`、`api/components` / `api/loras` 的 import 与相关 hook 调用)、
  `frontend/src/components/workflow/appEditorSchema.ts:77`(`seedvr2_model_select` 分支)、
  `frontend/src/components/overlays/ModelsOverlay.tsx`(:6-7 import、:73-77 hooks、
  :119-131 / :155 / :169-174 / :215-228 的 seedvr2 / component 预热·卸载·常驻分支与右键菜单项)、
  `frontend/src/components/overlays/DashboardOverlay.tsx:8,1336`(`useUnloadImageAdapters` 及其按钮)
- Test(修改):`frontend/src/api/engines.test.tsx`、`frontend/src/components/overlays/DashboardOverlay.test.tsx`、
  `frontend/src/components/services/ModelStatusBadge.test.tsx` —— 只删引用已删 hook / 端点的用例

**Interfaces:**
- Consumes: 无(前端先删,它调的后端端点此时还在,删完照样能跑)。
- Produces: 前端不再请求 `/api/v1/components*`、`/api/v1/loras`、`/api/v1/engines/seedvr2/*`、
  `/api/v1/engines/component/*`、`/api/v1/engines/unload-image-adapters`、`/api/v1/engines/image-cache`。
  Task 2 删这些端点时以此为前提。

- [ ] **Step 1: 准备**

```bash
cd frontend && npm ci && npx vitest run 2>&1 | tail -3
```
Expected:基线 74 files / 483 passed(2026-09-26 #755 合并后)。

- [ ] **Step 2: 整删文件 + 路由 + 导航**

```bash
git rm -q src/pages/Studio.tsx src/pages/studioWorkflows.ts src/pages/studioWorkflows.test.ts \
  src/api/components.ts src/api/components.test.tsx src/api/loras.ts \
  src/components/nodes/ComponentSelectWidget.test.tsx src/components/nodes/ComponentStatusHeader.test.tsx \
  src/components/nodes/ClipStackWidget.test.tsx
grep -rln "ComponentSelectWidget\|ComponentStatusHeader\|ClipStackWidget" src   # 对应组件文件也 git rm
```
`App.tsx` 删 `/studio` 那条 `<Route>` 与 `Studio` 的 lazy import;`IconRail.tsx` 删 `studio: '/studio'`
与 `{ id: 'studio', icon: Palette, label: '创作台' }`(`Palette` 若没别处用一并删 import)。

- [ ] **Step 3: 按 tsc 报错逐个清引用**

```bash
npx tsc -b 2>&1 | head -40
```
按报错删:`engines.ts` 七个 hook、`nodeRegistry.ts` / `workflow.ts` / `portColors.ts` 的类型成员、
`DeclarativeNode.tsx` / `appEditorSchema.ts` 的分支、`ModelsOverlay.tsx` / `DashboardOverlay.tsx`
的调用点。**每删一处重跑 `npx tsc -b`**,直到 exit 0。`DeclarativeNode.tsx` 里 `useLoadedAdapters`
以外所有来自 `api/components` 的 hook 调用(`useComponents` `useComponentState`
`useAllComponentStates` `useSeedvr2DitModels` …)都删,连带 `component_select` 等 widget 的
状态显示逻辑。

- [ ] **Step 4: lint + 测试**

```bash
npm run lint 2>&1 | tail -5 && npx vitest run 2>&1 | tail -5
```
红的测试:只删引用已删 hook / 端点的用例(`engines.test.tsx` 的 seedvr2/component 段、
`DashboardOverlay.test.tsx` 的 unload-image-adapters 段)。Expected:lint 0 errors;vitest 全绿,
文件数 ≈ 68。

- [ ] **Step 5: 构建**

```bash
npx vite build 2>&1 | tail -3
```
Expected:`✓ built`。

- [ ] **Step 6: Commit**

```bash
git add -A src && git commit -m "refactor(ui): 删创作台与组件/LoRA/SeedVR2 全部前端入口

自建图像引擎删除(spec 2026-09-21 改写版)第 1 步:前端先拆,它调的端点后端下一步删。
删 Studio 页与 /studio 路由、studioWorkflows 预设、api/components、api/loras、engines.ts 的
seedvr2/component/unload-image-adapters hooks、五种图像 widget 与三种 seedvr2 端口类型。
useLoadedAdapters / useUnloadAdapter 留给 TTS。"
```

---

### Task 2: 后端 API 层删除(组件/LoRA/SeedVR2/图像缓存端点、Celery 图像任务、WS 组件广播)

**Files:**
- Delete: `backend/src/api/routes/components.py`、`backend/src/api/routes/loras.py`、
  `backend/src/workers/image_worker.py`、`backend/tests/test_components_routes.py`、
  `backend/tests/test_components_state_routes.py`、`backend/tests/test_components_preload_e2e.py`、
  `backend/tests/test_component_preload_pr2a.py`、`backend/tests/test_component_resident_toggle_pr2b.py`、
  `backend/tests/test_component_unload_pr1.py`、`backend/tests/test_component_snapshot_pr3a.py`、
  `backend/tests/test_component_state_registry.py`、`backend/tests/test_component_state_wiring.py`、
  `backend/tests/test_seedvr2_preload_pr3.py`、`backend/tests/test_seedvr2_resident_pr2c.py`、
  `backend/tests/test_loras_route_cache.py`
- Modify:
  - `backend/src/api/main.py`:删 `:31-43` `_make_component_event_handler`、`:558-567` 组件索引
    启动扫描与 `app.state.component_index`、`:670-676` `ComponentStateRegistry` 与
    `on_component_event` 接线、`:1000-1001` components router 挂载、`:22` import 里的 `generate`
    与 `loras as loras_routes` 及其 `include_router`。**`image_orphan_reap_loop`(:328-350)留。**
  - `backend/src/api/routes/generate.py`:看 `TASK_MAP`(:20)—— 只有 `image`(和 `video`?)则
    **整删文件** + `src/workers/celery_app.py:21` 的 `image` 队列路由;若还有非图像任务只删
    `image` 项与 `/image` 端点。`understand.py` 不动(它是 VL 理解,不是生成)。
  - `backend/src/models/schemas.py:17-52`:删 `ImageGenerateRequest`(`VideoGenerateRequest`
    若只被 generate.py 用也删)。
  - `backend/src/api/routes/engines.py`:删端点 `/component/unload`(:303)、`/image-cache`(:487)、
    `/unload-image-adapters`(:542)、`/seedvr2/preload`(:588)、`/seedvr2/unload`(:608)、
    `/seedvr2/resident`(:647)、`_parse_component_name`(:682)、`/component/preload`(:689)、
    `/component/resident`(:722),以及 `from src.services.inference.image_seedvr2 import DEFAULT_DIT, DEFAULT_VAE`
    与 `from src.services.lora_scanner import …` 两处 import 及用到 `count_loras_for_arches` /
    `_inv_lora` 的代码。**`/loaded-adapters`(:508)与 `/loaded-adapter/unload`(:328)留。**
  - `backend/src/api/routes/monitor.py:449-459,480`:删 `pinned_ram_mb` 的聚合与响应字段
    (`from src.services.inference.pinned_stash import total_pinned_bytes` 一起删);
    `stash_ram_mb`(:460-461)**留**。
  - `backend/src/services/ws_hub.py:122-130`:删 `broadcast_component_state`。
  - `backend/src/services/model_deleter.py:264-272`:删 `component_scanner` / `lora_scanner` 的
    import 与两个 `invalidate_*` 调用。
  - `backend/src/api/startup_banner.py:98`:`_STACK_MODULES` 去掉 `"diffusers"`。
- Test(修改,只删用例):`tests/test_api_engines.py`、`tests/test_api_monitor.py`、
  `tests/test_health_endpoint.py`、`tests/test_model_delete.py`、`tests/test_workers.py`、
  `tests/test_celery_app.py`、`tests/test_startup_stack_check.py`

**Interfaces:**
- Consumes: Task 1 已保证前端不再调这些端点。
- Produces: `src/api` 不再 import `component_scanner` / `component_state` / `lora_scanner` /
  `image_seedvr2` / `pinned_stash`。Task 4 才能物理删这些模块。

- [ ] **Step 1: 删文件**

```bash
cd backend
git rm -q src/api/routes/components.py src/api/routes/loras.py src/workers/image_worker.py \
  tests/test_components_routes.py tests/test_components_state_routes.py tests/test_components_preload_e2e.py \
  tests/test_component_preload_pr2a.py tests/test_component_resident_toggle_pr2b.py tests/test_component_unload_pr1.py \
  tests/test_component_snapshot_pr3a.py tests/test_component_state_registry.py tests/test_component_state_wiring.py \
  tests/test_seedvr2_preload_pr3.py tests/test_seedvr2_resident_pr2c.py tests/test_loras_route_cache.py
```

- [ ] **Step 2: 改 main.py / engines.py / monitor.py / ws_hub.py / model_deleter.py / startup_banner.py / generate.py / schemas.py**

按上面 Files 清单逐处删。删完:
```bash
uv run ruff check . 2>&1 | tail -20
```
Expected:0 错(unused import 会指出漏删的 import,照着清)。

- [ ] **Step 3: 确认 API 层不再 import 图像卫星模块**

```bash
grep -rnE "component_scanner|component_state|lora_scanner|image_seedvr2|pinned_stash|image_worker" src/api src/workers src/services/ws_hub.py src/services/model_deleter.py
```
Expected:无输出。

- [ ] **Step 4: 跑受影响测试**

```bash
uv run pytest tests/test_api_engines.py tests/test_api_monitor.py tests/test_health_endpoint.py \
  tests/test_model_delete.py tests/test_workers.py tests/test_celery_app.py tests/test_startup_stack_check.py \
  tests/test_engine_catalog.py -q -n 4 2>&1 | tail -15
```
红的用例逐个看:断言的是被删端点/字段(`pinned_ram_mb`、`/seedvr2/*`、`/component/*`、
component_index)→ 删该用例;**其它原因红 → 是你漏删/误删了,修代码不修测试**。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor(api): 删组件库/LoRA/SeedVR2/图像缓存端点与 Celery 图像任务

自建图像引擎删除第 2 步(API 层)。删 routes/components、routes/loras、engines.py 的 9 个
组件/seedvr2/image-cache 端点、monitor 的 pinned_ram_mb、ws_hub 组件广播、main.py 的组件索引
启动扫描与 ComponentStateRegistry、Celery image_worker。/loaded-adapters 与 /loaded-adapter/unload
留给 TTS runner;stash_ram_mb(adapter 级 RAM stash)留。"
```

---

### Task 3: runner 层删除(图像派发分支、协议消息、client 方法、hardware.yaml image 组)

**Files:**
- Modify:
  - `backend/src/runner/runner_process.py`:删 `:84-85` `ImageOutputCache`、`:106-186` 的
    `_make_component_event_sender` / `_handle_preload_components` / `_handle_preload_component` /
    `_handle_set_component_resident` / `_handle_unload_component` / `_handle_preload_seedvr2`
    (**`_handle_set_model_resident` :167 留**),`_pipe_reader`(:227-296)里对应的 `isinstance`
    分支与 `:276-292` 的 `pinned_ram_mb` 快照(`stash_ram_bytes` :285 **留**),`_build_request`
    (:318-492)里 `upscale`(:328)与 `image`(:368)两个分支 —— 只留 `tts`(:484),
    末尾 `ValueError` 文案改成 `expected tts`;`_node_executor`(:495-765)里 `image` 的确定性
    L2 缓存(:520-543)、`upscale` / `image` 的 adapter 获取(:549-580)、latent 落盘(:721-727)、
    `image`/`upscale` 输出写盘(:728-760)。`_resolve_input_image_path`(:298)若无人再用则删。
  - `backend/src/runner/protocol.py`:删 `PreloadComponents`(:64)、`PreloadSeedVR2`(:75)、
    `PreloadComponent`、`SetComponentResident`、`UnloadComponent`、`ComponentEvent`(:205)、
    `Pong.loaded_components`(:190-194)与 `Pong.pinned_ram_mb`(:199),以及 `:221` 起的
    kind → class 映射里的对应项。`RunNode.node_type` 的类型若是 `Literal["image","tts","upscale"]`
    收成 `Literal["tts"]`。
  - `backend/src/runner/client.py`:删 `on_component_event`(:59, :188-193)、`preload_components`
    (:257)、`preload_seedvr2`(:274)、`preload_component`(:282)、`set_component_resident`、
    `unload_component`。
  - `backend/src/runner/runner_modelmanager.py`:grep `component|seedvr2|image`,只删图像相关覆写。
  - `backend/src/runner/supervisor.py`:grep `pinned_ram_mb|loaded_components`,删对应转发字段。
  - `backend/configs/hardware.yaml:45-49`:删 `id: image` 整组(**`llm` / `tts` 留**);顶部
    `:13` 注释「只有走 image / tts runner 子进程的节点」改成「只有走 tts runner 子进程的节点」。
  - `backend/src/services/status_sampler.py:41`:`COMPONENTS` 删 `("image", "图像 Runner")`,
    `compute_statuses()` 里按 runner 组算 image 状态的分支一并删(`tts` 那条留);
    `tests/test_status_sampler.py` 只删断言 image 组件的用例。状态页从此不再有「图像 Runner」。
- Delete: `backend/tests/test_seedvr2_runner_dispatch.py`、`backend/tests/test_runner_components_dispatch.py`、
  `backend/tests/test_runner_ipc_ideogram4.py`、`backend/tests/test_runner_l2_cache.py`、
  `backend/tests/test_runner_build_request_granular.py`、`backend/tests/test_granular_workflow_dispatch.py`、
  `backend/tests/test_inference_request_components.py`
- Test(修改,只删用例):`tests/test_runner_process.py`、`tests/test_runner_process_modelmanager.py`、
  `tests/test_runner_protocol.py`、`tests/test_runner_client.py`、`tests/test_runner_supervisor.py`、
  `tests/test_runner_models_aggregate.py`、`tests/test_runner_modelmanager.py`、
  `tests/test_tts_node_is_dispatch.py`(**这一个必须原样全绿,它锁 TTS 行为**)、
  `tests/fixtures/fake_runner.py`、`tests/chaos/test_worker_crash_storm.py`、
  `tests/integration/test_runner_resilience.py`、`tests/test_gpu_group_tp.py`、`tests/test_config.py`
  (hardware.yaml 组数断言)、`tests/test_resident_capacity.py`(只看 llm 组,应不受影响)

**Interfaces:**
- Consumes: Task 2 已删 API 侧对 `client.preload_*` 的调用。
- Produces: `src/runner` 不再 import `image_l2_cache` / `latent_storage` / `model_arch_adapter` /
  `pinned_stash`;`ModelManager.get_or_load_image_adapter` / `get_or_load_seedvr2_adapter` 不再有调用方。

- [ ] **Step 1: 先锁 TTS 基线**

```bash
cd backend && uv run pytest tests/test_tts_node_is_dispatch.py tests/test_runner_process.py -q 2>&1 | tail -3
```
记下 passed 数。

- [ ] **Step 2: 删测试文件,改 runner_process / protocol / client / supervisor / runner_modelmanager / hardware.yaml**

```bash
git rm -q tests/test_seedvr2_runner_dispatch.py tests/test_runner_components_dispatch.py tests/test_runner_ipc_ideogram4.py \
  tests/test_runner_l2_cache.py tests/test_runner_build_request_granular.py tests/test_granular_workflow_dispatch.py \
  tests/test_inference_request_components.py
```
按 Files 清单改。改完:
```bash
uv run ruff check src/runner configs && grep -rnE "image_l2_cache|latent_storage|model_arch_adapter|pinned_stash|get_or_load_image_adapter|get_or_load_seedvr2_adapter|PreloadComponents|PreloadSeedVR2|ComponentEvent" src/runner
```
Expected:ruff 0 错;grep 无输出。

- [ ] **Step 3: 跑 runner 相关测试**

```bash
uv run pytest tests/test_tts_node_is_dispatch.py tests/test_runner_process.py tests/test_runner_process_modelmanager.py \
  tests/test_runner_protocol.py tests/test_runner_client.py tests/test_runner_supervisor.py tests/test_runner_models_aggregate.py \
  tests/test_runner_modelmanager.py tests/test_gpu_group_tp.py tests/test_config.py tests/test_resident_capacity.py \
  tests/chaos tests/integration/test_runner_resilience.py -q -n 4 2>&1 | tail -15
```
`test_tts_node_is_dispatch.py` 的 passed 数必须与 Step 1 相同。其余红的:断言 image/upscale
派发、`pinned_ram_mb`、`loaded_components`、hardware 组数 == 3 的 → 删用例(组数断言改成
按 yaml 实际算不算「改断言」—— **算**,直接删那条用例);其它原因 → 修代码。

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor(runner): 删图像/超分派发分支、组件与 SeedVR2 协议消息、hardware.yaml image 组

自建图像引擎删除第 3 步(runner 层)。runner 子进程只剩 tts 派发;协议删
PreloadComponents/PreloadSeedVR2/PreloadComponent/SetComponentResident/UnloadComponent/
ComponentEvent 与 Pong.loaded_components/pinned_ram_mb;client 删对应方法。
hardware.yaml 删 image 组(Pro 6000 归 ComfyUI),llm/tts 不动。test_tts_node_is_dispatch 原样全绿。"
```

---

### Task 4: ModelManager / engine_catalog / model_scanner 的图像段删除

**Files:**
- Modify:
  - `backend/src/services/model_manager.py`(2494 行):删所有只服务图像路径的方法与常量 ——
    `_VRAM_EST_MB`(:1198)、`_component_bytes`(:1200)、`_component_need_mb`(:1216)、
    `_colocated_auto_footprint_mb`(:1248)、`_stream_footprint_mb`(:1291)、
    `_resolve_component_device`(:1398)、`_estimate_image_vram_mb`(:1431)、
    `_free_image_vram_on_card`(:1468)、`_guard_image_vram_per_card`(:1486)、
    `_explain_image_combo_key`(:1550)、`_derive_image_model_id`(:1582)、`_synthesize_image_spec`
    (:1606)、`loaded_components_snapshot`(:1664)、`get_or_load_image_adapter`(:1703)、
    `_image_stick_identity`(:1852)、`_get_or_load_anima_adapter`(:1864)、
    `get_or_load_seedvr2_adapter`(:1930)、`_l1_component_key`(:2032)、`set_component_resident`
    (:2054)、`unload_image_component`(:2065)、`_get_or_build_image_component`(:2094)、
    `_release_combo_components`(:2141)、`_stash_component`(:2179)、`_restore_component`(:2206)、
    `_trim_stash_lru`(:2225)、`preload_image_component`(:2252)、`_get_or_load_modular_adapter`
    (:2311)及其后到文件尾的图像内容;`:457-458` 的 `get_lora_paths` 注入;顶部
    `from src.services.inference.component_spec import …` 与 `from src.services.inference.image_modular import ModelManager…`
    等 import;`_models` entry 上只给图像用的字段(如 combo / components 池)。
    **留**:`stash_model`(:1104)、`stash_ram_bytes`(:1687)、`_stash_ram_reserve_bytes`(:2175)
    —— 但 `stash_model` 内若调 `_trim_stash_lru`(:1335 附近)把那一行删掉,不删方法本身;
    `_adapter_class`(:227)、`_instantiate_adapter`(:393)、`_resolve_placement`、TTL 卸载、
    resident、launch-params 合并、全部 LLM/ASR/embedding/TTS 逻辑。
    判据:**删完后 `grep -nE "image|component|seedvr2|anima|lora|pinned|combo" src/services/model_manager.py`
    只剩注释与 `image_orphan`/`stash_model` 这类非图像引用。**
  - `backend/src/services/engine_catalog.py`:删 `_component_loaded_index`(:41)、
    `seedvr2_catalog_entries`(:69)、`_COMPONENT_ROLES`(:104)、`_infer_arch`(:112)、
    `component_catalog_entries`(:128);`catalog_extra_engines`(:176)改成不再拼这三种
    条目 —— 若它此后只剩 `return []`,连函数与调用点(`engines.py` 里)一起删。
    **`type_filter` 里 `tts` 的分支别误删。**
  - `backend/src/services/model_scanner.py:33-66,194-210`:删 `_MEDIA_MODEL_SUBDIRS`、`media/` 桶
    depth-3 特例与 `model_index.json`(diffusers 目录)识别分支 —— 这些条目 `adapter` 永远为空,
    只在模型页当摆设。`model_metadata_service.py` 里若有同样的 `media` 特例一并删。
  - `backend/src/services/inference/base.py`、`cancel_flag.py`、`exceptions.py`、
    `src/runner/fake_adapter.py`:只有注释提到 diffusers(已 grep 确认无真 import),**不动**。
- Delete: `backend/tests/test_get_or_load_image_adapter.py`、`backend/tests/test_component_l1_cache.py`、
  `backend/tests/test_component_ram_stash.py`、`backend/tests/test_component_need_mb_sharded.py`、
  `backend/tests/test_component_expand.py`、`backend/tests/test_component_spec.py`、
  `backend/tests/test_component_scanner.py`、`backend/tests/test_lowvram_auto_stream.py`、
  `backend/tests/test_unified_mgmt_combo_arch_pr2.py`、`backend/tests/test_image_engine_selector.py`、
  `backend/tests/test_image_model_integration.py`、`backend/tests/test_lora_scanner.py`、
  `backend/tests/test_adapter_ram_stash.py`(先看:里面若有 **adapter 级** `stash_model` 的用例,
  把那几条挪进 `tests/test_model_manager_stash.py`(新建)再删文件;没有就整删)
- Test(修改,只删用例):`tests/test_engine_catalog.py`、`tests/test_model_scanner.py`、
  `tests/test_service_models.py`、`tests/test_service_autostart.py`、`tests/test_node_routing.py`、
  `tests/test_workflow_executor_dispatch.py`、`tests/test_workflow_executor_split.py`、
  `tests/test_workflow_executor_cached_event.py`、`tests/test_workflow_executor_is_deterministic.py`、
  `tests/test_workflow_node_highlight_stage_walk.py`、`tests/test_output_ttl_service_pr4.py`

**Interfaces:**
- Consumes: Task 2/3 已删所有对上述 ModelManager 方法的调用。
- Produces: `src/services/model_manager.py` / `engine_catalog.py` / `model_scanner.py` 不再 import
  `inference.image_*` / `arch_anima` / `component_*` / `lora_scanner` / `pinned_stash` /
  `model_arch_adapter`;Task 5 可以物理删这些模块。

- [ ] **Step 1: 删测试文件(先看 test_adapter_ram_stash.py)**

```bash
cd backend && grep -nE "def test_" tests/test_adapter_ram_stash.py
```
名字里带 `stash_model` / `adapter_stash` 且不 import 任何图像模块的用例 → 挪到新文件
`tests/test_model_manager_stash.py`(保留原断言原样);然后
```bash
git rm -q tests/test_get_or_load_image_adapter.py tests/test_component_l1_cache.py tests/test_component_ram_stash.py \
  tests/test_component_need_mb_sharded.py tests/test_component_expand.py tests/test_component_spec.py \
  tests/test_component_scanner.py tests/test_lowvram_auto_stream.py tests/test_unified_mgmt_combo_arch_pr2.py \
  tests/test_image_engine_selector.py tests/test_image_model_integration.py tests/test_lora_scanner.py \
  tests/test_adapter_ram_stash.py
```

- [ ] **Step 2: 改 model_manager.py / engine_catalog.py / model_scanner.py**

按 Files 清单删。每删一段跑一次:
```bash
uv run ruff check src/services/model_manager.py src/services/engine_catalog.py src/services/model_scanner.py
```
最后:
```bash
grep -nE "from src.services.(inference.(image_|arch_anima|component_spec|pinned_stash|model_arch_adapter|seedvr2)|component_scanner|component_state|lora_scanner)" src/services/model_manager.py src/services/engine_catalog.py src/services/model_scanner.py src/services/model_metadata_service.py src/api/routes/engines.py
```
Expected:无输出。`model_manager.py` 应缩到 ≈1300 行以内。

- [ ] **Step 3: 跑相关测试**

```bash
uv run pytest tests/test_model_manager*.py tests/test_engine_catalog.py tests/test_model_scanner.py tests/test_service_models.py \
  tests/test_service_autostart.py tests/test_node_routing.py tests/test_workflow_executor_dispatch.py tests/test_workflow_executor_split.py \
  tests/test_workflow_executor_cached_event.py tests/test_workflow_executor_is_deterministic.py tests/test_workflow_node_highlight_stage_walk.py \
  tests/test_output_ttl_service_pr4.py tests/test_launch_params_reach_adapter.py tests/test_resident_capacity.py -q -n 4 2>&1 | tail -15
```
红的:断言 upscale/component/lora 条目、image 类型模型、flux2/seedvr2 节点派发的 → 删用例;
`test_launch_params_reach_adapter` / `test_resident_capacity` **必须原样绿**(LLM 路径零变化)。

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "refactor(manager): 删 ModelManager 图像段、引擎库的 upscale/component/lora 条目、model_scanner 的 media 特例

自建图像引擎删除第 4 步。ModelManager 删 get_or_load_image_adapter / seedvr2 / anima /
组件池 / 组件级 RAM stash 全部方法(adapter 级 stash_model 留);engine_catalog 不再生成
upscale/component/lora 条目;model_scanner 删 media/diffusers depth-3 特例(这些条目 adapter
恒空)。LLM/ASR/embedding/TTS 路径零变化:launch-params / resident_capacity 测试原样绿。"
```

---

### Task 5: 物理删除引擎模块、卫星模块、节点包、image extra、manual smoke,全量绿

**Files:**
- Delete(`backend/src/services/inference/`):`image_modular.py`、`image_seedvr2.py`、`image_anima.py`、
  `arch_anima/`、`seedvr2_compat.py`、`seedvr2_vendor/`、`lcs_integration.py`、`lcs_vendor/`、
  `model_arch_adapter.py`、`quant_loaders.py`、`sigma_schedules.py`、`pinned_stash.py`、
  `image_l2_cache.py`、`component_spec.py`
- Delete(`backend/src/services/`):`component_scanner.py`、`component_state.py`、`lora_scanner.py`、
  `latent_storage.py`
- Delete(`backend/nodes/`):`flux2-components/`、`seedvr2/`、`lcs/`
- Delete(`backend/tests/`):`test_anima_engine_dispatch.py`、`test_arch_anima_predict2.py`、
  `test_flux2_checkpoint_clip_arch.py`、`test_flux2_checkpoint_resolve.py`、`test_flux2_comfy_lora_convert.py`、
  `test_flux2_components_loaders.py`、`test_flux2_components_sampling.py`、`test_ideogram4_dual_guider_workflow.py`、
  `test_image_adapter_card_guard.py`、`test_image_arch_registry.py`、`test_image_lora_cleanup.py`、
  `test_image_modular_wiring.py`、`test_image_sampler_ssim.py`、`test_image_l2_cache.py`、
  `test_latent_storage.py`、`test_model_arch_adapter.py`、`test_pinned_stash_unpin.py`、
  `test_quant_dequant_convert.py`、`test_quant_loaders.py`、`test_sigma_schedules.py`、`test_split_sigmas.py`、
  `test_seedvr2_device_resolve.py`、`test_seedvr2_node_package.py`、`test_seedvr2_skeleton.py`
- Delete(`backend/tests/manual/`):所有 `smoke_zimage*` `smoke_flux2*` `smoke_anima*` `smoke_ideogram4*`
  `smoke_seedvr2*` `smoke_lcs*` `smoke_image_*` `smoke_latent_relay.py` `smoke_modular_*`
  `smoke_schedulers.py` `smoke_scheduler_prod.py` `smoke_qwen_edit.py` `smoke_negative_cfg_prod.py`
  `smoke_true_cfg_prod.py` `smoke_single_file_prod.py` `smoke_per_component_device.py`
  `smoke_progress_cancel_e2e.py` `smoke_sampling_intervention.py` `smoke_load_checkpoint_dir.py`
  `smoke_granular_pr1.py` `smoke_fp8_compact.py` `smoke_cross_gpu_offload.py` `probe_zimage*`
  `spike_fit_3090.py` `spike_ideogram4_offload.py` `spike_modular_diffusers.py` `spike_quant_compact.py`
  `spike_single_file_assembly.py` `spike_true_cfg.py` `verify_ideogram4_whole_lowram.py` 及 golden 图目录。
  **`verify_wemm_embedding.py` 与 TTS/ASR 的 smoke 不动。**
- Modify:`backend/pyproject.toml:90-125` 删 `image = [...]` 整段与 `:49-50,128-135` 里只为
  diffusers 写的注释;`backend/uv.lock` 由 `uv lock` 重生成;`.github/workflows/ci.yml` 若有
  `--extra image` 则删(2026-09-26 grep 无,预期不用动)。

**Interfaces:**
- Consumes: Task 2–4 已让核心 `src/` 对这些模块的 import 归零。
- Produces: 仓库里 `grep -rn "diffusers" backend/src --include='*.py'` 只剩注释;`backend/nodes/`
  只剩 `image-color-match` `image-io` `qwen3-tts` `voxcpm2`。

- [ ] **Step 1: 删模块、节点包、测试、manual**

```bash
cd backend
git rm -rq src/services/inference/{image_modular.py,image_seedvr2.py,image_anima.py,arch_anima,seedvr2_compat.py,seedvr2_vendor,lcs_integration.py,lcs_vendor,model_arch_adapter.py,quant_loaders.py,sigma_schedules.py,pinned_stash.py,image_l2_cache.py,component_spec.py}
git rm -q src/services/{component_scanner.py,component_state.py,lora_scanner.py,latent_storage.py}
git rm -rq nodes/flux2-components nodes/seedvr2 nodes/lcs
git rm -q tests/test_anima_engine_dispatch.py tests/test_arch_anima_predict2.py tests/test_flux2_*.py tests/test_ideogram4_dual_guider_workflow.py \
  tests/test_image_adapter_card_guard.py tests/test_image_arch_registry.py tests/test_image_lora_cleanup.py tests/test_image_modular_wiring.py \
  tests/test_image_sampler_ssim.py tests/test_image_l2_cache.py tests/test_latent_storage.py tests/test_model_arch_adapter.py \
  tests/test_pinned_stash_unpin.py tests/test_quant_dequant_convert.py tests/test_quant_loaders.py tests/test_sigma_schedules.py \
  tests/test_split_sigmas.py tests/test_seedvr2_device_resolve.py tests/test_seedvr2_node_package.py tests/test_seedvr2_skeleton.py
cd tests/manual && git rm -q smoke_zimage*.py smoke_flux2*.py smoke_anima*.py smoke_ideogram4*.py smoke_seedvr2*.py smoke_lcs*.py \
  smoke_image_*.py smoke_latent_relay.py smoke_modular_*.py smoke_schedulers.py smoke_scheduler_prod.py smoke_qwen_edit.py \
  smoke_negative_cfg_prod.py smoke_true_cfg_prod.py smoke_single_file_prod.py smoke_per_component_device.py smoke_progress_cancel_e2e.py \
  smoke_sampling_intervention.py smoke_load_checkpoint_dir.py smoke_granular_pr1.py smoke_fp8_compact.py smoke_cross_gpu_offload.py \
  probe_zimage*.py spike_fit_3090.py spike_ideogram4_offload.py spike_modular_diffusers.py spike_quant_compact.py \
  spike_single_file_assembly.py spike_true_cfg.py verify_ideogram4_whole_lowram.py
ls   # 还剩什么?golden 图目录(名字含 golden/ssim)一并 git rm -r;不确定归属的读文件头再定
```

- [ ] **Step 2: pyproject 删 image extra,重生成 lock**

删 `[project.optional-dependencies]` 下 `image = [...]` 整段(:90-125)及注释。然后
```bash
cd backend && uv lock 2>&1 | tail -3 && git diff --stat uv.lock
```
Expected:lock 只减不增(diffusers / accelerate / peft 等消失)。**不要 `uv sync`。**

- [ ] **Step 3: 全库扫尾**

```bash
cd backend
grep -rnE "^\s*(from|import) .*(image_modular|image_seedvr2|image_anima|arch_anima|seedvr2|lcs_|model_arch_adapter|quant_loaders|sigma_schedules|pinned_stash|image_l2_cache|component_spec|component_scanner|component_state|lora_scanner|latent_storage)" src tests nodes
grep -rnE "^\s*(from|import) diffusers" src nodes
uv run ruff check . 2>&1 | tail -5
```
Expected:三条都无输出 / 0 错。

- [ ] **Step 4: 全量测试**

```bash
nvidia-smi --query-gpu=name,memory.used --format=csv,noheader && cd backend && uv run pytest tests -n 8 -q 2>&1 | tail -8
```
Expected:0 failed;passed ≈ 2361 − ~500。红的按前面各 task 的判据处理(删图像用例 / 修代码)。
`tests/test_data_plane_readonly.py` 必须绿。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "refactor(image): 物理删除自建图像引擎及全部卫星模块、节点包与 image extra

自建图像引擎删除第 5 步(最后一步代码)。删 image_modular/image_seedvr2/image_anima/arch_anima/
seedvr2_vendor/lcs_*/model_arch_adapter/quant_loaders/sigma_schedules/pinned_stash/image_l2_cache/
component_*/lora_scanner/latent_storage,节点包 flux2-components/seedvr2/lcs,pyproject 的 image
extra(diffusers 钉 commit 随之消失,uv.lock 重生成),24 个测试文件与 ~60 个 manual smoke。
代码存档见 tag image-engine-native-final。生产 venv 从未装过 image extra,不用动。"
```

---

### Task 6: 文档收尾(CLAUDE.md、spec 状态)

**Files:**
- Modify: `CLAUDE.md`「图像引擎 (image engine)」整节(从 `## 图像引擎 (image engine)` 到
  `## ComfyUI 桥 (comfy bridge)` 之前)、「GPU 放置」节里提到「图像路径经 `get_or_load_image_adapter`,
  仍会在执行期按需加载模型」的那句例外、「操作」节里「没有 diffusers(在 `image` extra,出图走
  ComfyUI 桥)」一句;`docs/superpowers/specs/2026-09-21-image-engine-to-node-packages-design.md`
  顶部「状态」行。

- [ ] **Step 1: CLAUDE.md**

把「图像引擎 (image engine)」整节替换为:

```markdown
## 图像引擎 —— 已删除(2026-09-xx)

自建原生图像引擎(Modular Diffusers / SeedVR2 超分 / Anima DiT / LCS 锐化)及其组件库、LoRA 库、
RAM pinned stash、latent 接力、`image` runner 组、创作台页面已于 2026-09-xx **整体删除**
(spec `docs/superpowers/specs/2026-09-21-image-engine-to-node-packages-design.md`)。出图**只走
ComfyUI 桥**。代码存档在 tag **`image-engine-native-final`**(删除 PR 的 base commit):
`git show image-engine-native-final:backend/src/services/inference/image_modular.py` 或
`git checkout image-engine-native-final -- <路径>`。`models/nous/media/` 的权重没删(ComfyUI 共用)。
```
（`xx` 填 Task 7 上线当天。）「数据面对放置只读」那条的**例外**段落改成只剩 `nodes/llm.py` 一项;
「操作」节里的括号改成「(出图走 ComfyUI 桥,仓库已无 diffusers 依赖)」。

- [ ] **Step 2: spec 状态行**

`**状态**:` 改成 `已实施(plan docs/superpowers/plans/2026-09-26-image-engine-removal.md)`。

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs && git commit -m "docs: CLAUDE.md 图像引擎节改为「已删除 + tag 找回方式」,spec 标已实施"
```

---

### Task 7: 收尾上线(主会话执行,不派 subagent)

- [ ] **Step 1: 打 tag(在删除 PR 的 base 上)**

```bash
cd /media/heygo/program/projects-code/repos/nous-engine && git fetch -q origin
git tag -a image-engine-native-final origin/master -m "自建图像引擎(Modular Diffusers / SeedVR2 / Anima / LCS)删除前的最后一版;见 docs/superpowers/specs/2026-09-21-image-engine-to-node-packages-design.md"
git push origin image-engine-native-final
```
若 worktree 分支的 merge-base 不是 `origin/master`(master 又前进了),先在 worktree
`git rebase origin/master`,再打 tag。

- [ ] **Step 2: 推分支、开 PR、等 CI 全绿**

```bash
cd ../nous-engine-image-removal && git push -u origin spec/image-engine-removal
gh pr create --base master --title "refactor: 删除自建图像引擎(代码存档 tag image-engine-native-final)" --body-file <(...)
gh pr checks --watch
```

- [ ] **Step 3: 上线(用户确认后)**

`./infra/autodeploy-guard.sh` 空闲 → `gh pr merge --rebase --delete-branch` → 主检出
`git checkout master && git pull --ff-only` → `./infra/deploy.sh`。**上线会重启后端**。

- [ ] **Step 4: 核验(spec §4 第 3 步)**

```bash
enginectl status
curl -s 127.0.0.1:8000/health | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d.get("load_failures"), d.get("comfy",{}).get("state"))'
journalctl -u nous-engine-backend --since -5min --no-pager | grep -iE "ImportError|ModuleNotFound|Traceback" | head
TOKEN=...; curl -s -H "Authorization: Bearer $TOKEN" 127.0.0.1:8000/api/v1/engines | python3 -c 'import json,sys; print({e.get("kind") for e in json.load(sys.stdin)})'
curl -s -H "Authorization: Bearer $TOKEN" "127.0.0.1:8000/v1/models?include_unready=1" | python3 -c 'import json,sys; print([(m["id"],m["ready"]) for m in json.load(sys.stdin)["data"]])'
```
Expected:三单元 active;`/health` ok、无 load_failures、comfy ok;journal 无 ImportError;
引擎库 kind 集合里没有 `upscale` / `component` / `lora`;`krea2` `ready=True`。最后真出一张图:
`POST /v1/services/krea2/predictions`(`Prefer: respond-async`)→ 轮询到 succeeded。
状态页「图像 Runner」组件应消失(`COMPONENTS` 里的 `("image", "图像 Runner")` 若在 Task 3
漏删,回去补)。

- [ ] **Step 5: 删 worktree,写 memory**

```bash
cd /media/heygo/program/projects-code/repos/nous-engine && git worktree remove ../nous-engine-image-removal
```
memory 新增一条:图像引擎已删、tag 名、「别再建议原生出图」。
