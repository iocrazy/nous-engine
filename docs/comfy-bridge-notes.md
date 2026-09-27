# ComfyUI 桥（comfy bridge）实现笔记

> 这份原本是 `CLAUDE.md` 的一个章节。它有 2500+ token，但只有在动 comfy bridge
> 时才需要——放在每次会话都加载的 `CLAUDE.md` 里不划算，2026-09-08 迁到这里。
> 内容一字未改，包含多起真机事故的结论，**改 `backend/src/.../comfy_bridge.py`、
> `comfy/style_options.py` 或 comfy 相关的 systemd 单元前必须先读完**。

- **模板即服务**:`POST /api/v1/comfy-templates {name, workflow}` 同时建
  `comfy_templates` 表行 + 一个 `ServiceInstance`(`source_type="comfy_template"`,
  服务名 = 传入的 `name`)。`PUT /api/v1/comfy-templates/{id}/mapping
  {exposed_params:[{key,label,type,comfy_node_id,comfy_input,...}]}` 定义哪些
  ComfyUI 节点输入可被参数化。`comfy_bridge.py::ComfyUIWorkflowNode.invoke` 取值是
  `data.get(key, m.get("default"))`——prediction `input` 里显式给的值优先,没给才落
  mapping 的 `default`;**只有 mapping 也没设 `default` 时才用 workflow 原值**(注册时
  冻结的快照)。想让某个 key 在不传 input 时也走某个值(如把时长压到最短),直接在
  exposed_params 里给它写 `default`,不必去改 workflow 导出文件本身。
- **图片模板 vs 视频模板**(2026-09-27):桥信封按**实际产物**分形 —— 有视频产物是老形状
  `{items, video_url, thumbnails, seed}`(一字未变);没视频、有图片时多一个 `image_url`
  (`video_url` 保留为 null,绝不拿图片冒充)。**`image_url` 只在图片形里出现**:任务面板
  (`execution_task_serialize._detect_image_meta`)按这个键是否存在判 image 任务。
  发布契约 `exposed_outputs` 在建模板时按输出节点推断(`VHS_VideoCombine`/`SaveVideo`/
  `SaveWEBM` → video,`SaveImage` → image,都不认识 → video 老默认),也可在
  `POST /api/v1/comfy-templates` 里显式给 `output_kind`。推断只在**新建**时跑;
  之前建的图片模板由 `docs/replications/upscale/fix-image-outputs.sh`(deploy.sh 会调)
  经 `PATCH /api/v1/services/{id}` 把 exposed_outputs 改成 `image_url`。终端节点 `video_output` 对图片信封
  要摘掉执行器 spread 塞进来的 `outputs=<图片 URL 字符串>`(见其实现注释)。
- **ComfyUI 执行错误必须落 failed**:`wait()` 只等 history 出现,不分成败;桥在收产物前查
  `status.status_str == "error"`(`comfy/outputs.history_error`),把 `execution_error` 的
  节点 + 异常原文带进 `task.error`。不查的话,出错前已写出的半截产物会被当成功返回。
- **文件类入参只收 data URI 或裸文件名**(`comfy/upload_inputs.py`),且**全部校验完才开始
  上传**(`_prevalidate_files`,免得后一个参数不合法时前一个已传成孤儿):data URI 必须 base64,
  `image`/`video`/`audio` 型要 mime 大类对得上;调用方给的非 data URI 字符串若是 URL、路径
  或 `[output]` 注解一律拒(桥不替调用方抓远程资源;注解会让 LoadImage 读 output 目录)。
- **可选文件参数未传 → 剪掉整条支路(`omit_when_empty`,2026-09-27)**:默认行为(上一条之外)
  是可选参数没传就 `continue`,**图里保留模板原值** —— 对 LoadImage 来说就是模板里烤死的占位图
  照样喂进模型(qwen21 编辑的「测试图 (17).png」、krea2 `ref_image` 的 `example.png`)。
  mapping 条目标 `omit_when_empty: true`(**只许文件类 + `required: false`**,PUT mapping 否则
  400)后,调用方没传(None 或空串)且 mapping 无 `default` 时,桥在上传/打补丁之后、submit
  之前调 `comfy/graph_prune.prune_graph`:删 `comfy_node_id` 指向的节点,顺连线级联 ——
  下游输入在 object_info 里 **optional 或是 autogrow 子键(键名带 `.`,如 `images.image_3`)
  → 只删该键**;**required → 节点也删、继续级联**;object_info 取不到 / 没声明该输入 →
  **保守删节点**(warning,宁可少一条支路也不喂占位图)。级联删到**产出端**(`output_node`
  且非预览/对比类;info 缺失时按 `FALLBACK_OUTPUT_CLASSES` 兜底)→ `GraphPruneError`
  (ValueError)落 failed、不提交:那个「可选」参数其实是输出链路必需的,是 mapping 配错了。
  object_info 走 `comfy/object_info.get_node_infos`:只取被剪节点**下游**那几种类型的
  `/object_info/{class}`,进程内缓存(成功 10 分钟 / 失败 30 秒),**绝不抛**(取不到 = None
  → 保守分支)。全部参数都传了就一次也不查。未标 flag 的老 mapping 行为一字不变。
  **autogrow 跳号**(传图 1、3 不传 2):ComfyUI 只按实际连了的子键展开 autogrow
  (`_expand_schema_for_dynamic`),`TextEncodeQwenImage21` 把收到的子键按序号排序后依次喂入,
  所以模型看到的是「第 1、第 2 张」—— 提示词里的「图三」此时指的是第二张。桥不重排序号
  (改了反而和调用方的参数名对不上),调用方按顺序连续传即可。
- **无音轨视频上传前补静音轨**(2026-09-27,`comfy/video_audio.ensure_audio_track`):
  `video/*` 的 data URI 在校验完、**任何上传之前**过一遍 ffprobe;没有音轨就
  `-c:v copy` 重封装 + 一条 `anullsrc` 静音 AAC(webm 用 libopus),视频不重编码;有音轨
  原字节上传。原因:VHS_LoadVideo 的 audio 输出接了下游(VOSR2 视频模板 `["18",2]` →
  VideoCombine 的 `audio`)时 VHS 懒提取音频,无音轨输入 ffmpeg 报 "Output file does not
  contain any stream" → "VHS failed to extract audio",整条渲染挂在 LoadVideo。放在桥里而
  不是改模板:所有带 LoadVideo→音频的模板一次兜住,且保留有音轨时的原声。它是**兜底**:
  ffprobe/ffmpeg 缺失、超时、失败一律 warning + 原字节上传,绝不让本来能跑的上传失败;子进程
  走 asyncio + 超时、并发限 2(`_FF_SEM`,与渲染 `_SEM` 无关、在它之前跑完),磁盘读写走线程
  (不堵事件循环)。触发看的是 data URI 的 `video/*` mime,file/media 泛型参数带视频也会处理。
  调用方字节不可信,两道防线各管一件:**`-f` 按扩展名钉死 demuxer**(mp4/mov → mov,
  webm/mkv → matroska,表外扩展名直接跳过)防 demuxer 混淆 —— 伪装成 mp4 的 ffconcat/HLS
  文本不会被自动探测成清单去读别的文件,只会解析失败退回原字节;**`-protocol_whitelist
  file`** 是纵深防御,只挡网络协议(SSRF),不挡读本地文件。
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
- **单元 `--listen` 是逐个点名网卡,不是通配符** —— 回环给后端桥(`NOUS_COMFY_URL`)
  + 本机浏览器,Tailscale 那个给「从别的机器开 ComfyUI 界面」。
  **具体地址在 `infra/network.env` 的 `NOUS_COMFY_LISTEN`(单一真相源,换网只改它)**,
  unit 里是 `--listen ${NOUS_COMFY_LISTEN}` + `EnvironmentFile=`(**无 `-` 前缀,
  fail-closed**:文件缺失就让单元起不来,而不是展开成空地址绑全网卡)。
  LAN(`192.168.8.x`)、docker0/br-*(`172.x`)、mihomo 的 Meta(`198.18.x`)一个口都不开。
  **别图省事改回 `0.0.0.0`**:ComfyUI 自身零鉴权,而它的节点能读写任意文件、执行自定义
  代码,绑通配符 = 把这台机器交给能路由到它的任何人(2026-09-07 收紧,#725/#727)。
  这条由 `tests/test_infra_privilege_boundary.py` **逐个地址**兜住(任一项是
  `0.0.0.0`/`::`/`*`/空就红,且回环不能摘 —— 摘了后端桥 Connection refused)。
  `main.py` 对 `--listen` 做 `split(",")`,每个地址
  各起一个 TCPSite;网卡晚起会 bind 失败,靠 `Restart=on-failure` 重试收敛
  (`After=tailscaled.service` 只能缓解 —— 网卡 up ≠ 地址已配)。
  **2026-09-21:ZeroTier 换成 Tailscale**,原 `10.0.0.10` 已不在任何网卡上。换网当时
  ComfyUI 进程还绑着旧 socket(`ss` 看得到 LISTEN 但收不到流量),属于「一重启就
  `EADDRNOTAVAIL`」的待爆状态 —— 换 tailnet / 重装节点后改 `infra/network.env` 即可。
- 真机 smoke(非 CI,需 sidecar 已跑 + 权重已入 ComfyUI models):
  `cd backend && uv run python tests/manual/smoke_comfy_h3.py --base http://127.0.0.1:8000
  --admin-token $ADMIN_TOKEN --workflow /path/to/xxx-api.json --mapping /path/to/mapping.json`
  ——建模板 → 设 mapping → 铸临时 key → 异步 prediction → 轮询 → 下载 mp4 →
  `ffprobe` 断言存在 video + audio 流 → 跑完自动清理(临时 key + 模板/服务),
  `--keep` 保留。
