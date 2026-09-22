# 把自建图像引擎插件化并拔出系统 — 设计

**日期**:2026-09-21
**状态**:设计已与用户逐节确认,待落实施计划
**动机**:ComfyUI 桥上线后,自建原生图像路径成为冗余;用户要求**保留代码但整体拔出系统**
(原话:「代码整理保留,不删除……之前说是插件版本,全部拔出系统」)。

---

## 1. 研判:桥是否已覆盖自建

结论:**是,可以拔出。** 五条证据,均已在真机/代码上查实。

### 1.1 对外契约零损失(最关键)

`/v1/images/generations`(OpenAI 兼容图像端点,`openai_compat.py:1450`)**不按 `category`
或 `source_type` 过滤**。它的解析条件只有三条:

```python
.join(ApiKeyGrant, ApiKeyGrant.service_id == ServiceInstance.id)
.where(
    ApiKeyGrant.api_key_id == api_key.id,
    ApiKeyGrant.status == "active",
    ServiceInstance.name == body.model,
)
```

加上需要 `workflow_snapshot`。而桥服务三条全满足 —— 实测 DB:

| name | source_type | category | has_snapshot |
|---|---|---|---|
| krea2 | comfy_template | app | **t** |
| minimax-h3-dialogue | comfy_template | app | **t** |
| minimax-h3-r2v | comfy_template | app | **t** |

**所以桥服务现在就能经 `/v1/images/generations` 调用**,拔掉自建不丢任何对外 API。

### 1.2 两条路共用同一执行核心

都经 `WorkflowExecutor` + `workflow_service_runner.run_published_workflow`。差别仅在工作流里
放哪个节点:自建放 `flux2-components` 的 dispatch 节点(→ `get_or_load_image_adapter`),
桥放 `comfy_bridge` 节点(→ HTTP 到 ComfyUI)。**拔掉的是一个节点实现,不是一套架构。**

### 1.3 能力逐项可替代

| 自建服务 | ComfyUI 侧 |
|---|---|
| `studio-upscale`(SeedVR2 超分) | `ComfyUI-SeedVR2_VideoUpscaler` 自定义节点**已安装**;权重就在 `models/nous/media/SEEDVR2/` |
| `studio-text-to-image` / `studio-image-edit` / `studio-angle` | Z-Image / Flux2 / Qwen-Image-Edit 权重均在 ComfyUI 模型树 |
| `ideogram4` / `wanwu-qianyi` / `img-flux2` / `img-wikeeyang` | 同上,且桥另有 275 风格与自定义节点生态 |

**权重从来只有一份**:自建路径读的就是 `models/nous/media/`,即 ComfyUI 的模型目录。

### 1.4 这 8 个服务已经是死的

2026-09-21 清理 API key 后(36 → 2),保留的 `nous-app` / `comfyui-vlm` **一个图像服务都没授权**,
现在没有任何 key 调得动它们。

### 1.5 维护税真实存在

`diffusers` 钉死 git commit `784fa62…`,注释自己写明 Modular Diffusers 是
**EXPERIMENTAL(0.38.0.dev0),API 跨 commit 就破**;每次 bump 必须先跑
`tests/manual/smoke_image_ab.py` 的 SSIM golden 回归。只要它还在主依赖里,
升级主环境就一直被它牵制。

### 1.6 额外收益

拔出后 CLAUDE.md「数据面对放置只读」不变式的**例外之一消失**
(现文:「图像路径经 `get_or_load_image_adapter`,仍会在执行期按需加载模型」)。

---

## 2. 现状依赖图(四层)

| 层 | 位置 | 处置 |
|---|---|---|
| **1 节点包** | `nodes/flux2-components`(生成 dispatch)、`nodes/seedvr2`(超分 dispatch) | **随引擎拔出** |
| | `nodes/image-io`、`nodes/image-color-match` | **留** — 纯 CPU inline 节点,只认 `image_url`,桥的出图同样受益 |
| **2 runner 派发** | `runner_process.py` 的 84 / 108-126 / 178-181 / 373 / 522 / 545 / 568 | **改造成走接缝** |
| **3 ModelManager 钩子** | `get_or_load_image_adapter`(`model_manager.py:1678`)、`get_or_load_seedvr2_adapter`、anima 装配(:1843-1893) | **核心工作:抽成插件接口** |
| **4 引擎实现** | `image_modular.py`(1978)、`image_seedvr2.py`(500)、`arch_anima/anima.py`(368)、`image_anima.py`(203)、`seedvr2_compat.py`(42)、`seedvr2_vendor/` | **搬进包** |

### 必须留在核心的共用件(删了会搞挂桥)

- `image_output_storage.py` —— `nodes/comfy_bridge.py:36` 直接 `from ... import write_image`
- `image_files.py` 路由 —— 出图分发
- `src/services/nodes/image.py`(`image_output`,28 行纯展示汇点,透传 `image_url`)
- `image_l2_cache.py` —— runner 侧输出缓存,不碰 diffusers/torch,桥的出图同样受益
- `workflow_service_runner.py` / `predictions.py` / `WorkflowExecutor` / `nodes/` 框架含 `comfy_bridge.py`
- `/v1/images/generations` 端点本身
- `models/nous/media/` 全部权重(ComfyUI 的树)

---

## 3. 设计:插件接缝

**在 `ModelManager` 上开 adapter-provider 注册表**,核心不再认识具体引擎。

- 核心只保留泛化入口(形如 `mm.get_or_load_adapter(kind, spec)`);`kind` 由包在被
  `scan_packages` 扫描到时注册(`flux2` / `seedvr2` / `anima`)
- 包不在 → `kind` 未注册 → runner 派发返回**结构化的「节点不可用」**,
  **而不是 ImportError 崩掉**。这是本改造**唯一的新行为**
- 核心 src 对 `diffusers` / `seedvr2_vendor` / `arch_anima` 的引用归零
- `pyproject.toml` 删整个 `image` extra;`diffusers` 钉死 commit 搬进
  `nodes/flux2-components/requirements.txt`(包机制已支持:`POST /nodes/packages/{name}/install_deps`
  跑 `uv pip install -r`,是**增量**安装,不同于会裁包的 `uv sync`)

---

## 4. 测试划分

| 去向 | 文件数 | 用例数 | 代表 |
|---|---|---|---|
| **随包搬** | 15 | ~191 | `test_image_modular_wiring` 58、`test_image_adapter_card_guard` 24、`test_seedvr2_skeleton` 23 |
| **留核心** | 7 | ~94 | `test_image_output_storage` 36、`test_workflow_publish_image` 15、`test_images_generations` 14、`test_image_io_node_package` 12、`test_seedvr2_runner_dispatch` 10、`test_image_l2_cache` 6、`test_image_node` 3 |

### ⚠️ 两件必须同时做的事,否则会悄悄丢掉测试覆盖

1. **把 `nodes/*/tests/` 加进 CI 收集路径** —— 现状 `pyproject.toml` 无对应 `testpaths`、
   `ci.yml` 也没有 `nodes/` 路径。191 个用例直接搬过去 = 等于删掉。
   包装着时 CI 照跑、包拔掉时一起消失,这个语义才是对的。
2. **新增「接缝退化」测试,放核心** —— 包不存在时派发返回结构化错误而非崩溃。
   这是唯一的新行为,现在零覆盖,而它正是"拔出后系统还能不能正常跑"的判据。

---

## 5. 落地顺序与回滚点

四阶段,每阶段结束都是可上线、可回滚的状态。

1. **开接缝**(不搬代码)
   ModelManager 加注册表;三个引擎仍在核心内注册;runner 改走注册表。
   行为零变化,全量测试应全绿 —— 纯重构。**回滚点 A**。
2. **加退化路径 + 测试**
   注册表查不到 `kind` → 结构化错误;补接缝测试;CI 加 `nodes/*/tests/`。**回滚点 B**。
3. **搬家**
   引擎代码 → `nodes/flux2-components/` 与 `nodes/seedvr2/`;`diffusers` → 包的
   `requirements.txt`;`pyproject.toml` 删 `image` extra;15 个测试文件跟着搬。
   **回滚点 C** —— 此时功能仍在,只是位置变了,可先真机出一张图确认完好。
4. **拔出**
   删 8 个服务 + 对应画布工作流;`DELETE /nodes/packages/{flux2-components,seedvr2}`;
   CLAUDE.md 图像引擎节改写成「已插件化,默认不装」。

把"拔出"放最后的好处:前三步任何一步出问题都能停;第 3 步做完可先验证功能完好,再决定拔不拔。

---

## 6. 已知风险

1. **`seedvr2_vendor/` 内嵌第三方源码搬家** —— 最易出岔子的一块。它保留了上游 NumZ 的
   `src/{core,models,common,data}` + `configs_7b`/`configs_3b` 目录结构,且
   `seedvr2_compat.py` 明写「**必须在 import seedvr2_vendor 任何模块前调用**」的兼容 patch
   依赖 `get_script_directory()` 的相对路径。搬家后 import 路径与该 patch 都要重写,
   第 3 阶段须单独验证。
2. **分发体积** —— vendor 进包后 `install_zip` / `install_git` 的包体显著变大。
3. ~~`image-color-match` 的 `color-matcher` 依赖可能随 `image` extra 一起被删~~
   —— **已查实,不是风险**:`color-matcher>=0.6.0` 在 `pyproject.toml:31`,属于
   **base dependencies**(`[project.optional-dependencies]` 在 34 行才开始),
   删 `image` extra 不影响它。

4. **anima 的归属** —— 它不是独立引擎,而是**图像 adapter 路径内的一个架构分支**:
   `model_manager.py:1812` 按 arch registry 判 `adapter == "anima"` 才走
   `_get_or_load_anima_adapter`,其余(flux2 / z-image / qwen-edit)走 Modular 路径。
   所以 `image_anima.py` + `arch_anima/` **随 `flux2-components` 包一起搬**,
   不单开包;arch registry 的 `adapter` 字段即是包内的架构选择器。

---

## 7. 明确不做

- 不删 `models/nous/media/` 任何权重(ComfyUI 共用)
- 不动画布工作流子系统本身(`nodes/` 框架、`llm.py`、`audio.py`、`comfy_bridge.py` 都留)
- 不动 `/v1/images/generations` 端点
- 不在本次处理 `PATCH /engines/{name}/launch-params` 的 404 bug(另案)
