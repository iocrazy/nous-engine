# nous-engine — Claude / AI agent notes

Single-admin inference infra (推理算力层). Repo/product renamed **nous-center →
nous-engine** (裸 `nous-` 前缀让给上层平台;systemd 单元全套 `nous-engine-*`,CLI
`enginectl`). Production deploy = `backend serve frontend/dist` on `:8000`, reachable **only
on this host / LAN / Tailscale (tailnet 名 `heygo-ubuntu`;地址见 `infra/network.env`)** — the Cloudflare tunnel (`api.iocrazy.com`,
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
  仓库是 public,**不挂 self-hosted runner、不轮询**:扳机就是合并本身,Mac 经 Tailscale
  ssh 过去。不经 ship 的合并(网页/dependabot)不会自动上线,下次 ship 一并带上。
- **Python 3.13.14**(`.python-version`,2026-09-09 起;CI/生产/开发机统一)。生产 `.venv` 只装
  `--extra inference`,**没有 diffusers**(在 `image` extra,出图走 ComfyUI 桥),不是 bug。
  **uv 的 venv 目录绝不能 `mv` 改名**:`bin/*` 入口脚本是绝对路径 shebang,改名后 `bin/uvicorn`
  ENOENT、unit 起不来(2026-09-09 生产停 4.5 分钟)。要换 venv 就在最终路径 `rm -rf .venv &&
  uv sync --extra inference`(轮子全在缓存,秒级);deploy.sh 重启前会跑 `uv run uvicorn
  --version` 兜住这类坏入口。旧 3.12 环境留在 `backend/.venv-312`,只有换回 `.venv` 名才能用。
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

- 两张卡(PCI 序,`src/api/main.py` setdefault 了 `CUDA_DEVICE_ORDER=PCI_BUS_ID`):
  `cuda:0` = RTX PRO 5000 Blackwell 71.7GiB(`GPU-f4334111-b8d2-4df3-ea0f-6177698f8ce9`)
  承**全部推理服务**(LLM/ASR/embedding/OCR);`cuda:1` = RTX PRO 6000 Blackwell 95.6GiB
  (`GPU-d24ed424-5712-55e9-9b95-77d997ac80dc`)**ComfyUI 独占**,systemd 单元用 UUID 钉卡
  (索引随插拔漂移,UUID 不会;MOSS ASR 子进程同理用 UUID)。2026-09-20 原先两张 RTX 3090
  (旧索引 0/2,NVLink 互联)已物理拔除,`hardware.yaml` 的跨卡组 `llm-tp` 随之删除,现在
  只有 `llm`/`image`/`tts` 三个**单卡组**,**没有任何多卡 group**;显示输出走主板
  ASPEED BMC,不占 N 卡(旧的「GPU 0 驱动显示器,腾空前别用于 TP」约束已作废)。
  **异构卡拼不成 TP 组**(Pro 5000 ≠ Pro 6000,见下方同型号校验),本机 tp 恒为 1 ——
  以下 TP 相关代码路径原样保留,只是现在走不到,除非将来买入同型号第二张卡。
- **放置决策只在 `ModelManager._resolve_placement` 一处**。适配器(vLLM/SGLang)只执行
  传下来的 `device`/`gpus`,**绝不自己换卡** —— 适配器自作主张换卡会造成「预算按 A 卡算、
  `CUDA_VISIBLE_DEVICES` 钉 B 卡」的启动期 OOM,manager 记的落卡也和真实占用对不上。
- **显式 `gpu`/`gpus` 是硬约束**:装不下就 `ModelLoadError`(信息里给可用组的建议),
  不自动搬。只有没有任何显式放置的模型才走自动选卡/选组。
- **模型级 `gpus: [...]`**(与单卡 `gpu` 并存,给了就以它为准)= 张量并行跨这组卡,
  `tp = len(gpus)`(显式 `tensor_parallel_size` 只能**收窄**)。落点:models.yaml 的
  `gpus:`、`model_runtime_overrides.gpus`(JSONB **三态**:NULL=未覆盖 / `[]`=显式清空组 /
  非空列表=组;没有 `[]` 这个哨兵,YAML 配了组的模型永远退不出组)、
  `PATCH /api/v1/engines/{name}/gpu` 的 body `{"gpus":[...]}`(单卡仍是 `?gpu=N`)。
  本机现在没有同型号第二张卡可组,这条路径当前打不着,字段/语义原样保留。
- **候选组的权威来源是 `configs/hardware.yaml`**(经 `GPUAllocator._build_groups` 解析),
  **不是**代码枚举卡的组合。`nvidia-smi topo -m` 只用于 nvlink 的**校验/补缺**。yaml
  没声明多卡 group(本机现状)→ `GET /api/v1/gpu/groups` 返回空 + hint,菜单里没有
  「组合」项。要跨卡先去 yaml 加同型号组。
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
  `tests/test_data_plane_readonly.py` 静态锁住这五个路由模块;常驻集合按落卡汇总必须
  放得进 `configs/hardware.yaml` 的容量减 `DEFAULT_RESERVED_GB`,由
  `tests/test_resident_capacity.py` 在 CI 兜住(常驻不自洽合 PR 前就红,不等上线)。
  **例外(不在本不变式内)**:画布工作流的 `predictions` 经 `nodes/llm.py`、图像路径经
  `get_or_load_image_adapter`,仍会在执行期按需加载模型(待单开 spec)。
- **GPU 0(Pro 5000)常驻名单**(2026-09-20 迁移后实测,`vram_mb` 见各模型 yaml 注释):
  MOSS ASR 9000 + qwen3.8-27B-AWQ 25500 + WeMM-Embedding-4B 24800 = 59300 MiB ≈
  57.9 GiB,上限 72 − 4 = 68 GiB(见上条容量测)。**Unlimited-OCR 不设 resident**(四样全
  常驻要 72.08 GiB 超卡),走 `ttl_seconds: 3600` 按需加载 —— 数据面不懒加载(见上条),
  闲置卸载后调用方会先吃一个 503 `model_not_ready`,得从控制面手动重新加载,不会自动补起。
- **各模型 `gpu_memory_utilization` 实测地板**(Pro 5000 71.12 GiB 开机可见口径,换卡/
  改 `max_model_len`/`max_num_seqs` 必须重新标定,别照抄旧卡数字):qwen3.8 = 0.35
  (0.34 实测 KV 差 0.44 GiB 起不来,真地板)、WeMM-Embedding-4B = 0.34、Unlimited-OCR =
  0.20。**用户硬决定:qwen3.8-27B-AWQ 永不搬到 Pro 6000**(哪怕以后 Pro 6000 有空余量),
  别再建议换卡。
- **Pascal 卡在本机不可用**(2026-09-20 排查结论,别再重查):驱动 595 是开源内核模块,
  要求 GPU 自带 GSP(Turing 及以后才有),Pascal(GTX 1060 等)probe 直接失败;而
  Blackwell 必须 ≥595、Pascal 只支持 ≤580,一台机器只能装一个驱动版本,互斥无解。
- **`model_runtime_overrides` 有个孤儿陷阱**:`qwen3_asr` 那行 `gpu=1` 残留至今(该
  模型 2026-07-21 被 MOSS ASR 取代后 yaml 已移除,engine 不在 registry 里,API 会返回
  `Unknown engine`,当前不生效,已裁定不动)。**将来若重新加回 `qwen3_asr` 的 yaml,这行
  会立刻把它钉到 ComfyUI 独占的 GPU 1 上** —— 加回前必须先改掉或删掉这条 override。

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

## 启动参数的运行时覆盖 (launch-params)

- `PATCH /api/v1/engines/{name}/launch-params` 把启动参数写进 DB 的
  `model_runtime_overrides.params`(JSONB,只存被覆盖的键),**不写 yaml** ——
  写 git 跟踪的文件会被 `git checkout/pull` 冲掉(同 `resident` 端点的理由)。
  值为 `null` = 清除该覆盖回退 yaml。改动**下次 load 才生效**(`applied` 恒 false)。
  配套 `GET .../launch-params` 回 `{effective, overridden}`:前者是「下次 load 会用的值」
  (已过 `_apply_runtime_overrides` 深合并),后者标出其中哪几个键来自 DB 覆盖。
  UI 入口在**模型页右键菜单**「启动参数…」(`ModelsOverlay.tsx`,紧挨「显存预算…」)。
- 白名单是 `src/config.py` 的 `LAUNCH_PARAM_WHITELIST`(**一个对象,两处消费**:写端点
  与 `ModelManager._instantiate_adapter`;放在配置层是为了不让服务层反向 import API 层):
  `max_model_len` / `max_num_seqs` / `max_num_batched_tokens` /
  `enable_prefix_caching` / `dtype` / `quantization`。键名之外**还校验值域**
  (三个数字键必须是 >0 的 int 且排除 bool;`enable_prefix_caching` 必须 bool;
  两个字符串键必须非空 str)—— 否则前端一个空输入框就能把 `0` 写进 DB 且退不出来。
- **`gpu_memory_utilization` 与 `tensor_parallel_size` 刻意不可覆盖**,PATCH 到会 400:
  util 是「占该卡总量」的比例、换卡必须重算(2026-09-11 事故:改落卡忘改 util,
  模型在 Pro 6000 上抓 53.3GiB 把 ComfyUI 挤到 19GiB),显存一律走
  `PATCH /engines/{name}/vram-budget`(存**绝对 GiB**,加载时按实际那张卡换算);
  tp 是放置结论,由 `_resolve_placement` 定。
- ⚠️ **合并点有两个,少一个就是 no-op**:
  1. `config.py::_apply_runtime_overrides` —— 喂 `load_model_configs()`,也就是 GET 端点
     报给 UI 的 `effective`。**只管显示。**
  2. `ModelManager._instantiate_adapter` —— 每次 load 时合并进适配器构造参数。**只管行为。**

  为什么不能只在 registry 的 `_load` 里合并:那里只在**启动时**跑一次、`ModelSpec` 是
  frozen 快照,PATCH 之后就算 unload + load 用的还是旧值,非重启后端不可 —— 与端点
  「unload + load 生效」的承诺对不上。2026-09-22 这支的 Critical 正是漏了第 2 点:
  端点写了库、GET 照报「已生效」,适配器收到的还是 yaml 原值,整个特性对 `models.d/`
  定义的模型全程是 no-op,**而且 UI 是绿的**(比旧的 404 更难发现)。
  加新的覆盖键时,**两处都要过一遍**。
- ⚠️ `_apply_runtime_overrides` 的 `copy_before_write=True` 时必须**连 `params` 一起
  新建 dict** —— 只浅拷外层的话,`params` 子 dict 与 `model_scanner` 的 TTL 缓存共享
  同一对象,原地改会写穿(症状:改了 30 秒看不到,或改一次污染此后所有读)。
  `vllm_args` 子 dict 同理,深一层(见下条)。
- ⚠️ **prefix caching 有两条配法**:`params.enable_prefix_caching`(适配器 kwarg)与
  `params.vllm_args["enable-prefix-caching"]`(透传,`merge_vllm_args` 同名时**以
  vllm_args 为准**)。本机 qwen3.8 两个变体走的是后者,于是:
  - 读端点**两处都读**,否则 UI 复选框显示「没开」而实际开着,用户一点就反转真实状态;
  - 显式覆盖 `enable_prefix_caching` 时,`config.drop_prefix_caching_vllm_alias()`
    会把 `vllm_args` 里的两个别名**摘掉**(返回新 dict,不原地 pop —— 同上条写穿),
    读写两条路径共用这一个实现。不摘的话这个开关是**单向的**:开得了、关不掉、不报错。

  加白名单键时先查有没有同类的双写路径(另外 5 个键今天没有 yaml 用到,是潜伏项)。
- `kv_cache_dtype` **不在白名单**:本机没有 nvcc,fp8 KV 会在 FlashInfer JIT 处起不来
  (2026-09-21 两次实测),加进去等于给一个「点了会起不来」的按钮。

## Embedding 模型

- 模型都在 **`embedding/<MODEL>`**(LOCAL_MODELS_PATH 下的顶层桶,depth-2)。
  2026-09-11 从 `text/embedding/` 提上来:`text/` 下只有 embedding 一个 bucket,那层
  纯属多余,`model_scanner` 与 `model_metadata_service` 里各一段 depth-3 的 `text`
  特例一并删掉了。多模态 embedding 也放这里(`Qwen3-VL-Embedding-2B` 从 `vl/` 搬入),
  判据是**任务是 embedding**,不是模态。
- **WeMM-Embedding-4B / 9B**(tencent,Qwen3.5 base,文本+图像+视频 → 4B 2560 维 /
  9B 4096 维,MRL)。架构 `Qwen3_5ForConditionalGeneration` 本机 vLLM 0.28 原生支持,
  **不走 trust_remote_code**(仓库里的 `modeling_wemm_embedding.py` 是 transformers /
  sentence-transformers 路径用的,vLLM 用不到)。
- **调用方必须走 `messages`,别传 `input` 字符串**:WeMM 的
  `embedding_chat_template.jinja` 在末尾追加 `<embedding>` token,而 vLLM 的
  `/v1/embeddings` **只有带 `messages` 时才套 chat template**;传 `input` 是裸
  tokenize。2026-09-11 真机实测(`tests/manual/verify_wemm_embedding.py`):同一句话
  两条路算出的向量余弦只有 **0.909(4B)/ 0.945(9B)**,且 messages 路的同义/无关
  区分度更好(4B 0.837 vs 0.231,input 路 0.785 vs 0.194)。**两条路混用会污染同一个
  向量库** —— 建库和检索必须固定同一条。Qwen3-Embedding 系列没有这个 token,不受影响。
- **`gpu_memory_utilization` 别按权重大小猜**(2026-09-11 踩过):WeMM-4B 权重才
  8.6GiB,但 vLLM 启动要按 `max_num_seqs × max_model_len` 带**多模态 dummy 输入**跑
  profiling,视觉塔激活峰值把自身消耗顶到 ~21GiB。util 给 0.15 直接
  `No available memory for the cache blocks`(Available KV cache memory: **-6.78GiB**)。
  标定值写在两份 yaml 的注释里,改之前先读。调 `max_num_seqs` 要同步抬 util。
- 改这两个模型的 yaml 或升 vLLM 后,跑
  `uv run python tests/manual/verify_wemm_embedding.py {4b|9b}`(真模型/GPU,非 CI)——
  CI 有 Popen 护栏起不了真 vLLM,配置能不能起、向量对不对只靠这个 standalone 脚本。

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
  `os.environ.setdefault` 或命令前缀)。否则 torch 默认 FASTEST_FIRST 按算力排序,两张卡
  **对调**:`cuda:0` 成了 Pro 6000、`cuda:1` 成了 Pro 5000。于是 `SMOKE_DEVICE=cuda:1`
  本意是 ComfyUI 那张空闲的 Pro 6000,实际落到跑着全部常驻模型的 Pro 5000 上 —— 常驻已占
  ~58GiB,大模型直接 OOM,还会把推理服务一起拖下水。(2026-09-20 两卡改造前这里的形状是
  `cuda:1` 变成 24G 的 3090 装 9B 直接 OOM;卡换了,坑还在,只是换了个样子。)生产经
  `src/api/main.py` 已 setdefault,但 standalone 脚本不经它、且 `uv` 不 load `.env`。
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
