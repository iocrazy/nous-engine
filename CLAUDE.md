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

- Backend: systemd services. `sudo ./infra/systemd/install.sh`,
  then `journalctl -u nous-engine-backend -f` for logs. Don't `nohup ... & disown`.
  一键管控 `enginectl status|up|down|restart|logs`(装到 `/usr/local/bin/enginectl`)。
- Admin secrets: `./infra/security/gen-admin-secrets.sh > /tmp/secrets && cat /tmp/secrets`
  then paste into `backend/.env`. Three values: `ADMIN_PASSWORD` (browser cookie login),
  `ADMIN_SESSION_SECRET` (HMAC key), `ADMIN_TOKEN` (CLI bearer).
- Production frontend changes need `cd frontend && npm run build` after merge —
  backend serves `frontend/dist/`, not the source.
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

- **模板即服务**:`POST /api/v1/comfy-templates {name, workflow}` 同时建
  `comfy_templates` 表行 + 一个 `ServiceInstance`(`source_type="comfy_template"`,
  服务名 = 传入的 `name`)。`PUT /api/v1/comfy-templates/{id}/mapping
  {exposed_params:[{key,label,type,comfy_node_id,comfy_input,...}]}` 定义哪些
  ComfyUI 节点输入可被参数化。`comfy_bridge.py::ComfyUIWorkflowNode.invoke` 取值是
  `data.get(key, m.get("default"))`——prediction `input` 里显式给的值优先,没给才落
  mapping 的 `default`;**只有 mapping 也没设 `default` 时才用 workflow 原值**(注册时
  冻结的快照)。想让某个 key 在不传 input 时也走某个值(如把时长压到最短),直接在
  exposed_params 里给它写 `default`,不必去改 workflow 导出文件本身。
- **长任务走 respond-async**:`POST /v1/services/{name}/predictions`(注意前缀是
  `/v1/`,不是 `/api/v1/`)带 `Prefer: respond-async` → 202 `{id,status:"starting"}`,
  轮询 `GET /v1/predictions/{id}` 到终态(`succeeded|failed|canceled`)。鉴权跟
  `/api/v1/*` 管理端点是两套:`/v1/*` 带了 `Authorization` header 优先走 M:N
  `InstanceApiKey` 校验,校验失败(含 `ADMIN_TOKEN` 这种压根不是 InstanceApiKey 的
  bearer)会退回 admin-session 校验(cookie 或 `Authorization: Bearer $ADMIN_TOKEN`
  都认)——脚本化调用(无浏览器 cookie)可以直接用 `ADMIN_TOKEN` 当 bearer(**`/v1/audio/
  transcriptions` 例外**:`_auth_transcriptions` 带了 Authorization 就直连
  `verify_bearer_token_any`、不回落 admin-session,`ADMIN_TOKEN` 会被拒为 Invalid API
  key,脚本化调用要铸 InstanceApiKey —— 2026-09-05 实测),也可以
  先用它经 `POST /api/v1/keys {label, service_ids:[<service 数字 id>]}` 铸一把授权给该
  service 的 M:N key,再拿它的 `secret` 调 predictions(两条路都通)。
- **选项可依赖另一个参数**:`exposed_params` 项上写
  `options_depends_on: "<另一个 key>"` + `options_source: "comfy_styles"`,该字段的
  选项清单就在**运行期**按依赖参数的当前值拉(krea2:`styles` 随 `style_pack` 切换),
  不再只认注册时冻结的那份静态 enum。链路:mapping → `constraints.options_*` →
  schema 的 `x-options-depends-on`/`x-options-source` → 前端 `DependentOptionField`
  按 `GET /api/v1/comfy/styles?pack=` 拉 + 后端 `comfy/style_options.py`
  (10 分钟 TTL 缓存)按包校验。**静态 enum 照旧写**,是 sidecar 不可达时的兜底
  (拿不到清单只 warning + 退回静态,绝不让预测 500)。`options_depends_on` 指向不存在
  的 key 在 PUT mapping 时就拒(400 `validation_error`)。
  krea2 那批风格包的缩略图是 sidecar 侧**相对路径**,经
  `GET /api/v1/comfy/style-image?path=<文件路径>` 代理:**只收文件路径、不收 URL**,
  路由写死 + httpx `params=` 传参。别改回"收 src 再转发 + 前缀白名单"——httpx 合并
  相对 URL 会归一化点段,`/easyuse/../history` 到 sidecar 就是 `/history`,白名单形同
  虚设(2026-09-03 审查实测)。缓存头是 `private`(端点要 admin 鉴权,`public` 会让
  cloudflared/中间缓存把字节回给未鉴权者)。
- 动态清单的取数(`style_options.py`)有三条闸门:① 依赖参数的值必须先落在它自己的
  静态 enum 里(没 enum 则只认 default)才会去打 sidecar —— 这段跑在
  `validate_service_input` **之前**,不设闸等于让任何持 key 的人拿随机包名驱动出站请求
  + 撑大进程内缓存;② 缓存 256 条上限、按插入序驱逐,失败与空结果只缓存 30s(负缓存),
  成功缓存 10 分钟;③ 预校验取数用 5s 短超时(列清单那条仍是 15s)。
  **「取不到」(None,退回静态 enum)与「取到了但为空」([],空白名单全拒)是两回事**,
  后者退回默认包的静态 enum 正是本机制要防的错配。
- **`multiple` 与静态 options 正交**:`multiple` 说的是**值的形状**(逗号分隔串),
  跟「有没有在注册时冻结一份静态 enum」无关 —— 只声明依赖、不带 options 的 mapping
  同样要落 `constraints.multiple` / schema 的 `x-multiple`,否则运行期会拿动态清单去
  整串比对 `'a,b'`,多选必 422。
- **文件类参数(image/file/audio/video/binary/media)不能声明 `options_depends_on`
  /`options_source`**:PUT mapping 直接 400 `validation_error`。它的值是上传的文件,
  挂上动态清单等于给上传字段发一份风格名白名单,上传必 422。schema 侧对文件类也不
  输出 `x-options-*`(双保险,兜老数据)。
- **已知限制**:`_thumbnail_url` 把 sidecar 的相对缩略图改写成 admin-only 的代理地址
  (`/api/v1/comfy/style-image`)。如果编辑器把某个动态包的 options **冻结进 mapping**,
  `/v1/services/{name}/schema` 里的 `x-option-meta[].image` 对只持 `InstanceApiKey` 的
  第三方就是 401 —— 缩略图渲不出来(值本身照常可用)。前端 Playground 走 admin 会话不
  受影响。要给第三方也能看的缩略图,得先给这个代理端点开一条 InstanceApiKey 能过的路。
- **等待渲染时必须能被打断**(2026-09-03 事故):`ComfyClient.wait` 除了轮询
  `/history/{prompt_id}`,每轮还查 `/queue` 并按 `should_abort` 探测取消 ——
  ① 桥节点传入"这个 ExecutionTask 在 DB 里是不是 cancelled",**每 5 轮(≈10s)才查
  一次**(4 小时的渲染每 2s 打一次 DB = 7200 次纯轮询查询,晚 10 秒发现取消无差别);
  ② prompt 连续 3 轮既不在 `queue_running` 也不在 `queue_pending`、且 `/history` 仍没有
  → 判 "sidecar 已丢弃该任务"(重启/清队列)抛错。`/queue` 打不通算**状态未知**,
  不判丢失(重启窗口本身就是打不通)。**别把这两条摘掉**:ComfyUI 的 `/interrupt`
  只在节点之间生效,卡在某节点内部(那次是等一个 CLOSE_WAIT 的 HF 下载)时救不回来,
  没有这两条 wait 会占着 `_SEM` 干等到 `NOUS_COMFY_TIMEOUT`(默认 4 小时),
  所有 comfy 服务一起堵死,只能重启后端。抛出的 `ComfyError` 落 failed;若 DB 已是
  cancelled,`workflow_runner` 的 honor-cancelled 分支保住 canceled 终态。
  `GET /api/v1/comfy/health` 的 `running_render`(`{task_id, held_seconds}`,空闲 None)
  就是给下次排障一眼看出"谁占着信号量、占了多久"的。
- env 三件套:`NOUS_COMFY_URL`(sidecar 地址,默认 `http://127.0.0.1:8188`)、
  `NOUS_COMFY_TIMEOUT`(渲染等待上限,默认 14400s)、
  `NOUS_COMFY_DOWNLOAD_TIMEOUT`(产物下载超时,默认 120s)。
- **渲染串行化 = 单进程部署前提**:桥节点(`comfy_bridge.py`)靠一个模块级
  `asyncio.Semaphore(1)`(`_SEM`)保证「sidecar 一次只服务一个渲染」,这假设 uvicorn
  跑无 `--workers`(生产就是这样)。上了多 worker/多进程部署,每个进程各有自己的
  `_SEM`,互不认识,这条不变式就破了——需要先换成跨进程锁(Postgres advisory lock
  之类)才能加 `--workers`。
- sidecar 是独立 systemd 单元 `nous-engine-comfyui`,**首装** disabled(装机时模板
  路径 `/opt/comfyui` 多半还没铺,`enginectl restart` 不会强启一个禁用单元——2026-08-10
  设计修正,见 `infra/systemd/enginectl`)。启用前置只剩一条:核对单元里的
  `CUDA_VISIBLE_DEVICES` 跟目标卡对得上(`infra/systemd/nous-engine-comfyui.service`)。
  `enginectl status|up|down|restart|logs comfyui` 纳管。
  **GSP 缓解脚本已不再是前置**(2026-09-08 复核):驱动已从 `infra/gpu/README.md` 点名的
  595.71.05 升到 **595.91.07**;`setup-gpu-mitigations.sh` **从未上机**(persistence 三卡
  全 Disabled、`DynamicPowerManagement=3` 即 GC6 仍开、`nous-gpu-guard` 单元不存在),
  而 2026-07-01 至今内核日志**零 Xid、零 FULLCHIP_RESET**,期间 PRO 6000 一直带着常驻
  模型跑、ComfyUI 也钉在这张卡上。用户 2026-08-11 亦明确确认该问题不再出现。
  `infra/gpu/README.md` 留档仅供**复发时**排查,别再当安装前置推荐。
  **「首装」不是「每次装」**:`install.sh` 对已 `enabled` 的 comfyui 保持不动(#727)。
  这里以前是无条件 `systemctl disable`,每次重装都把「前置条件已核对过」的结论推翻,
  且不带 `--now` → 进程照跑、当场零征兆,**要等下次重启才发现 sidecar 没回来**
  (2026-09-07 就这么踩了一次:16:37 跑 install.sh,21:31 重启才显形)。
- **单元 `--listen` 是逐个点名网卡,不是通配符**:`127.0.0.1,10.0.0.10` —— 回环给后端桥
  (`NOUS_COMFY_URL`)+ 本机浏览器,ZeroTier 那个给「从别的机器开 ComfyUI 界面」。
  LAN(`192.168.8.x`)、docker0/br-*(`172.x`)、mihomo 的 Meta(`198.18.x`)一个口都不开。
  **别图省事改回 `0.0.0.0`**:ComfyUI 自身零鉴权,而它的节点能读写任意文件、执行自定义
  代码,绑通配符 = 把这台机器交给能路由到它的任何人(2026-09-07 收紧,#725/#727)。
  换机器要改那个硬编码的 ZeroTier IP。`main.py` 对 `--listen` 做 `split(",")`,每个地址
  各起一个 TCPSite;ZT 网卡晚起会 bind 失败,靠 `Restart=on-failure` 重试收敛
  (`After=zerotier-one.service` 只能缓解 —— 网卡 up ≠ 地址已配)。
- 真机 smoke(非 CI,需 sidecar 已跑 + 权重已入 ComfyUI models):
  `cd backend && uv run python tests/manual/smoke_comfy_h3.py --base http://127.0.0.1:8000
  --admin-token $ADMIN_TOKEN --workflow /path/to/xxx-api.json --mapping /path/to/mapping.json`
  ——建模板 → 设 mapping → 铸临时 key → 异步 prediction → 轮询 → 下载 mp4 →
  `ffprobe` 断言存在 video + audio 流 → 跑完自动清理(临时 key + 模板/服务),
  `--keep` 保留。
- 前端联调注意:`npm run build` 实际是 `prebuild`(跑 `wasm:build`,需要
  `wasm-pack`)+ `tsc -b && vite build`——worktree 里没装 `wasm-pack` 会卡在 prebuild;
  `tsc -b`(project references 构建)不能拿裸 `npx tsc` 替代,后者不认 composite
  引用会报一堆假错误。

## Memory

User's persistent memory lives in `~/.claude/projects/.../memory/MEMORY.md`. Index
of feedback/preferences/project context. Auto-loaded into context. Read it before
making framing decisions.
