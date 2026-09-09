# nous-engine — Claude / AI agent notes

Single-admin inference infra (推理算力层). Repo/product renamed **nous-center →
nous-engine** (裸 `nous-` 前缀让给上层平台;systemd 单元全套 `nous-engine-*`,CLI
`enginectl`). Production deploy = `backend serve frontend/dist` on `:8000`, reachable **only
on this host / LAN / ZeroTier (10.0.0.10)** — the Cloudflare tunnel (`api.iocrazy.com`,
`nous-engine-cloudflared`) was **retired 2026-09-06 by the user's decision**: 不要再给
nous-engine 加任何对外暴露的入口(隧道/反代/端口映射),上层平台 nous-app 有自己的隧道,
它只经内网调 nous-engine。vite dev (`:9999`) is **local-only** for frontend hot reload.

## API endpoint vs UI route — DON'T MIX

| Need to hit | Use |
|---|---|
| Backend API | `/api/v1/keys`, `/api/v1/engines`, `/api/v1/services`, `/api/v1/workflows`... |
| UI route (browser address bar) | `/api-keys`, `/services`, `/workflows`, `/models`... |

The UI route `/api-keys` is the React Router path users see; the backend endpoint is
`/api/v1/keys` with no `api-` prefix. Calling `/api/v1/api-keys` returns 404.

## Operational

- **合并 + 上线一条命令**(2026-09-09):在 Mac 上 `./infra/ship.sh <PR号>` —— 等 CI 全绿 →
  squash 合并 → ssh 到生产机跑闸门(`infra/autodeploy-guard.sh`:开关关着 / ComfyUI 渲染
  进行中 → **只合并不上线**,exit 5)→ `deploy.sh` → 校验生产 HEAD 含本次 merge commit。
  被闸门拒过之后 `./infra/ship.sh --deploy-only` 重试;`enginectl autodeploy off|on` 是开关。
  仓库是 public,**不挂 self-hosted runner、不轮询**:扳机就是合并本身,Mac 经 ZeroTier
  ssh 过去。不经 ship 的合并(网页/dependabot)不会自动上线,下次 ship 一并带上。
- Backend: systemd services. `sudo ./infra/systemd/install.sh`,
  then `journalctl -u nous-engine-backend -f` for logs. Don't `nohup ... & disown`.
  一键管控 `enginectl status|up|down|restart|logs`(装到 `/usr/local/bin/enginectl`)。
- Admin secrets: `./infra/security/gen-admin-secrets.sh > /tmp/secrets && cat /tmp/secrets`
  then paste into `backend/.env`. Three values: `ADMIN_PASSWORD` (browser cookie login),
  `ADMIN_SESSION_SECRET` (HMAC key), `ADMIN_TOKEN` (CLI bearer).
- Production frontend changes need `cd frontend && npm run build` after merge —
  backend serves `frontend/dist/`, not the source.
  `npm run build` 实际是 `prebuild`(跑 `wasm:build`,需要 `wasm-pack`)+
  `tsc -b && vite build` —— worktree 里没装 `wasm-pack` 会卡在 prebuild;
  `tsc -b`(project references 构建)不能拿裸 `npx tsc` 替代,后者不认 composite
  引用会报一堆假错误。
- Dev backend (manual, not systemd): `backend/scripts/dev-serve.sh` — sources `.env`
  (uv won't), runs uvicorn, tees stdout to `backend/logs/backend-dev.log` (50MB rotate).
  Structured request/audit/app/frontend logs go to the **main PostgreSQL DB**
  (4 tables via `src/models/log_entry.py`, written through `log_store.py`'s async
  queue + single batch consumer; view via `/api/v1/logs/*` or the frontend
  LogsOverlay) regardless of stdout. There is no longer a separate SQLite
  `log_db` — one DB (spec `docs/superpowers/specs/2026-06-10-log-db-merge-into-postgres-design.md`).
  Production stays on journald for raw stdout.

## Testing

- Backend tests run with `ADMIN_PASSWORD=""` forced in `tests/conftest.py` so the
  admin gate is off during the suite. Don't unset that.
- SPA catch-all is disabled in tests via `NOUS_DISABLE_FRONTEND_MOUNT=1`
  (also set in conftest). If you add a new test that registers routes after
  `create_app()`, this matters — otherwise the catch-all swallows them.
- **测试只跑 PostgreSQL,没有 sqlite**(2026-09-02 起,全局只有一种数据库)。conftest
  按 `DATABASE_URL`(`.env` 或环境变量,角色需 `CREATEDB`)建一个
  `nous_test_<worker>_<hex>` 临时库,建表一次、每个用例后 `TRUNCATE … RESTART IDENTITY
  CASCADE`、进程退出时 DROP。**新加 `src/models/xxx.py` 必须同步加进 conftest 顶部的
  `import src.models.xxx` 列表**,否则建表时不存在、用到它的用例报 UndefinedTable。
  被 SIGKILL 的跑批不走 atexit,残留库手动
  `DROP DATABASE "nous_test_…"`。别用 `NOUS_TEST_USE_REAL_DB=1`(直连真库的逃生口,会污染生产数据)。
- **并行**:`uv run pytest tests -n 8`(pytest-xdist,每 worker 各自临时库)本地约 65s,
  串行约 6 分钟;`-n 16` 不会更快。CI 用 `-n auto`。PG `max_connections=100` 是 worker
  数上限的天花板,别在 48 核机器上 `-n auto`。
- **测试绝不能真起推理服务碰 GPU**(2026-09-02 事故:本地生产 venv 装着 vllm,
  `test_vllm_adapter` 真起 `python -m vllm.entrypoints…`、`CUDA_VISIBLE_DEVICES` 被 adapter
  覆盖成 "0",多 worker 并发初始化 CUDA 把 RTX 3090 驱动跑挂,nvidia-smi 全 D 态,后端 API
  与桌面一起冻死,只能重启)。conftest 有 Popen 护栏:argv 含 `vllm.entrypoints` /
  `sglang.launch_server` / `sgl-omni` 直接 AssertionError,只有 `NOUS_RUN_GPU_TESTS=1`
  放行。写涉及 adapter.load() 的测试一律 mock `subprocess.Popen`。派 subagent 跑测试时,
  两个人**别同时**跑全量(runner 子进程用例会互相拖挂),且先 `nvidia-smi` 确认驱动活着。

## Performance

- `/api/v1/engines`, `/api/v1/services`, `/api/v1/workflows` are wrapped with
  `@cached("prefix", ttl=30)` from `src/api/response_cache.py`. Any new write
  path that mutates these lists must call `invalidate("prefix")` (cross-resource
  writes pass multiple prefixes — see `workflow_publish.py`).
- ETag is computed on the serialized body bytes, not the dict — keeps it stable
  across non-deterministic dict/set iteration order.

## GPU 放置 / 张量并行 (GPU groups)

- 本机三张卡(PCI 序):`cuda:0` = RTX 3090 24G(**驱动显示器**)、`cuda:1` = RTX PRO 6000
  96G、`cuda:2` = RTX 3090 24G。0 与 2 之间有 NVLink(`nvidia-smi topo -m` 显示 NV4)。
  生产经 `src/api/main.py` setdefault 了 `CUDA_DEVICE_ORDER=PCI_BUS_ID`。
- **放置决策只在 `ModelManager._resolve_placement` 一处**。适配器(vLLM/SGLang)只执行
  传下来的 `device`/`gpus`,**绝不自己换卡** —— 适配器自作主张换卡会造成「预算按 A 卡算、
  `CUDA_VISIBLE_DEVICES` 钉 B 卡」的启动期 OOM,manager 记的落卡也和真实占用对不上。
- **显式 `gpu`/`gpus` 是硬约束**:装不下就 `ModelLoadError`(信息里给可用组的建议),
  不自动搬。只有没有任何显式放置的模型才走自动选卡/选组。
- **模型级 `gpus: [0, 2]`**(与单卡 `gpu` 并存,给了就以它为准)= 张量并行跨这组卡,
  `tp = len(gpus)`(显式 `tensor_parallel_size` 只能**收窄**)。落点:models.yaml 的
  `gpus:`、`model_runtime_overrides.gpus`(JSONB **三态**:NULL=未覆盖 / `[]`=显式清空组 /
  `[0,2]`=组;没有 `[]` 这个哨兵,YAML 配了组的模型永远退不出组)、
  `PATCH /api/v1/engines/{name}/gpu` 的 body `{"gpus":[0,2]}`(单卡仍是 `?gpu=N`)。
- **候选组的权威来源是 `configs/hardware.yaml`**(经 `GPUAllocator._build_groups` 解析),
  **不是**代码枚举卡的组合 —— 那份 yaml 记着运维约束(GPU 0 驱动显示器,腾空前别用于 TP)。
  `nvidia-smi topo -m` 只用于 nvlink 的**校验/补缺**。yaml 没声明多卡 group →
  `GET /api/v1/gpu/groups` 返回空 + hint,菜单里就没有「组合」项。要跨卡先去 yaml 加组。
- 组的硬性校验(`topology.validate_gpu_group`,HTTP 与 YAML 路径共用):≥2 张、去重、
  卡存在、**同型号**、大小是 **2 的幂**(tp 要整除注意力头数)。显示卡只 warning
  (与单卡路径一致)。YAML 里写了非法组 → log error 并忽略该字段,不阻塞启动。
- **绝不存在「不钉卡」的启动分支**(`inference/_placement.py` 的三条不变式)。要 tp>1
  却没有组 → 退单卡 + `logger.error`,不拿"全部可见卡"顶上(那正是 2026-09-02 事故的
  形状)。没有落卡结论时钉 `CUDA_VISIBLE_DEVICES=""`。
- 只有 `supports_gpu_group = True` 的适配器(vLLM / SGLang)能吃组;别的引擎配组会被
  API 400 拒,manager 也只对它们做组预留(否则是幻影预留 + `loaded_gpus` 撒谎)。
- 显存预算(`topology.group_budget_gb`,**唯一实现**)按组内**最小** total/free 算,
  不是求和 —— `gpu_memory_utilization` 是每卡比例。预算端点与适配器同源(nvidia-smi 的
  MB),别一边用 torch 的 `gpu_summary()` 一边用 nvidia-smi。
- `gpu`/`gpus` 优先级的唯一实现是 `topology.resolve_gpus(cfg_or_spec)`;
  API 响应里 **`gpu` 永远是主卡 int、`gpus` 是唯一的列表字段**(单卡为 None)。
- **模型放置只能由控制面改变;数据面对放置只读**(spec 2026-09-05 engine-app-boundary)。
  这里的「数据面」是**五个 LLM 兼容路由模块**:`openai_compat` / `anthropic_compat` /
  `ollama_compat` / `responses` / `context_cache`。未加载的模型一律即刻 503
  `model_not_ready`,不在请求路径上加载;`/v1/models` 与 Ollama 的 `/api/tags` 只列
  已加载的 model 类服务(共用 `routes/_readiness.py`,发现到的 == 现在就能调的);
  `resident: true` 是**唯一**的常驻手段,已发布工作流不再钉住模型。
  `tests/test_data_plane_readonly.py` 静态锁住这五个路由模块。
  **例外(不在本不变式内)**:画布工作流的 `predictions` 经 `nodes/llm.py`、图像路径经
  `get_or_load_image_adapter`,仍会在执行期按需加载模型(待单开 spec)。
  常驻集合按落卡汇总必须放得进 `configs/hardware.yaml` 的容量减 `DEFAULT_RESERVED_GB`,
  由 `tests/test_resident_capacity.py` 在 CI 兜住(常驻不自洽合 PR 前就红,不等上线)。

## vLLM 参数透传 (`params.vllm_args`)

- 模型 yaml 的 `params.vllm_args` 是一个 dict,把**任意** vLLM 长参数透传到子进程命令行
  (适配器只自己拼固定的那几个:tp/max_model_len/quantization/dtype/max_num_seqs/
  prefix-caching/runner)。键名不带 `--`,下划线连字符都认(统一归一成连字符)。
  渲染规则(`llm_vllm.render_vllm_args`,**唯一实现**):bool True → 只加 `--flag`;
  bool False / None → 不加;dict/list → `json.dumps` 成**一个**参数值
  (`--speculative-config` 就吃 JSON 串);其余 → `--flag value`。
- **同名以 vllm_args 为准**:`merge_vllm_args` 把适配器自己拼的那份(flag + 它的值)
  从 argv 里摘掉再追加 + `logger.warning`。同一个 flag 出现两次 vLLM 行为不确定。
- **安全边界,写了直接 ValueError(构造期,不是 load 期)**:`--model`(路径由 manager
  按 `LOCAL_MODELS_PATH` 解析)、`--port`(manager 分配)、`--device` 与
  `--tensor-parallel-size`(落卡与 tp 只由 `_placement` 决定,见上一节三条不变式 ——
  **tp 是放置结论的一部分,不是调优旋钮**;要收窄用 `params.tensor_parallel_size`,
  要换组改 `gpus`)。`vllm_args` 影响不到 `CUDA_VISIBLE_DEVICES`,也改不了 tp。
- 落地示例:`configs/models.d/qwen3_8_27b_abliterated_awq.yaml`
  (MTP 投机解码 `speculative-config` 实测 83 → 111 tok/s;`reasoning-parser: qwen3`
  把思考分离到 `reasoning_content`,**不是关思考**)。

## 图像引擎 (image engine)

- 引擎只剩一套 = `ModularImageBackend`(`image_modular.py`,Modular Diffusers)。
  迁移已完成,**legacy 自写 `ImageSampler`/`image_diffusers.py`/`image_sampler.py` 已删**
  (#128-132);`NOUS_IMAGE_ENGINE` 环境变量已无 legacy 选项。Anima 自定义 DiT 走
  `image_anima.py`。spec
  `docs/superpowers/specs/2026-05-22-image-engine-modular-diffusers-design.md`。
- **Modular Diffusers 是 experimental**;`diffusers` 在 `pyproject.toml` **钉死 commit**。
  改 `image_modular.py` **或升 diffusers 前,必须跑**
  `tests/manual/smoke_image_ab.py`(真模型/GPU,非 CI)并确认 SSIM ≥ 0.97 + 出图正确,
  再 bump commit。CI 跑不了真模型(conftest mock torch + 无 GPU),引擎正确性只靠这个
  standalone smoke。该 smoke 现在是 **golden 回归比对**(legacy 没了,不再是 legacy/modular
  A/B):重生成 modular 出图 → SSIM 比保存的 golden 图。
- **standalone smoke 必须在 import torch 前设 `CUDA_DEVICE_ORDER=PCI_BUS_ID`**(脚本顶部
  `os.environ.setdefault` 或命令前缀)。否则 torch 默认 FASTEST_FIRST 把 Pro 6000 排到
  `cuda:0`、`cuda:1` 变成 24G 的 3090 → `SMOKE_DEVICE=cuda:1` 装 9B 模型直接 OOM。生产
  经 `src/api/main.py` 已 setdefault,但 standalone 脚本不经它、且 `uv` 不 load `.env`。
- `diffusers.modular*` 的 import **只允许在 `image_modular.py`**(`_import_modular()`
  一处)——experimental API 变更时 blast radius 限一文件。

## ComfyUI 桥 (comfy bridge)

sidecar 是独立 systemd 单元 `nous-engine-comfyui`,后端经 `NOUS_COMFY_URL`
(默认 `http://127.0.0.1:8188`)调它;「模板即服务」= `comfy_templates` 行 +
`ServiceInstance`,长任务走 `POST /v1/services/{name}/predictions` + `Prefer:
respond-async`。

**动 `comfy_bridge.py`、`comfy/style_options.py` 或 comfy 相关 systemd 单元前,
先读完 [`docs/comfy-bridge-notes.md`](docs/comfy-bridge-notes.md)** —— 那里有
渲染串行化(`_SEM` 单进程前提)、可中断等待(2026-09-03 堵死事故)、动态选项三条闸门、
style-image 代理为什么只收文件路径(2026-09-03 审查)等一整套不变式,每一条都是
真机踩出来的,凭直觉重写必然重蹈。

## Memory

User's persistent memory lives in `~/.claude/projects/.../memory/MEMORY.md`. Index
of feedback/preferences/project context. Auto-loaded into context. Read it before
making framing decisions.
