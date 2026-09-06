# systemd 部署

把 `backend (uvicorn)`(以及状态页、巡检、备份、ComfyUI sidecar)跑成开机自启的 systemd
服务,取代 `nohup ... & disown`。机器重启 / shell 退出后服务仍在跑。

> **公网隧道已退役(2026-09-06)**:`nous-engine-cloudflared`(Cloudflare 隧道 `api.iocrazy.com`)
> 已从仓库删除,`install.sh` 会主动 `disable + rm + mask` 它。nous-engine **只在本机 / 局域网 /
> ZeroTier(10.0.0.10)可达**,这是用户的明确决定:不要再给它加任何对外暴露的入口。

## 一次性安装

```bash
sudo ./infra/systemd/install.sh
```

## 发版 / 上线 — 一条命令

```bash
./infra/deploy.sh
```

拉 master → 前端 build → 重启后端 → 自检,每步 fail-loud。**别用 sudo 跑整个脚本**(git/npm
用 root 会在仓库造 root 属主文件);脚本以你的身份跑,重启那步用 `sudo systemctl restart
nous-engine-backend` —— 装过 `install.sh` 的机器**免密**(`nous-deploy.sudoers` 只放行这一命令,不存
任何密码);没装则正常弹密码。关键防呆:build 后校验 `frontend/dist` 时间戳真被更新,否则中止不重启 ——
杜绝「build 没成却以为上线了」(后端 serve 的是编译好的 `frontend/dist`,不是源码)。
⚠️ 重启会卸掉所有已加载模型,vLLM ~30s 重载。

## 一键管控 — enginectl

装机会同时把 `enginectl` 放到 `/usr/local/bin`,全栈一条命令(取代分别敲多条 systemctl):

```bash
enginectl up        # 拉起全栈(DB→后端→状态)并打印启动自检 banner
enginectl down      # 停应用栈(后端/状态/comfyui);postgresql 保持运行
enginectl restart   # 重启应用栈 + 打印重启报告(见下)
enginectl status    # 各 unit active? + 端口 + ZeroTier/ComfyUI,一屏
enginectl logs [u]  # journalctl -f(u 缺省 backend;可 status/healthprobe/netprobe/comfyui/postgresql)
```

启停内部用 `sudo`(会提示密码);`status`/`logs` 只读无需 sudo。

`up`/`restart` 的报告依次给:重启了哪些 unit(comfyui 未 enabled 会明说跳过)→ 后端**真的能收请求**的耗时(不是 `is-active`:那个早几秒就翻真;判据是
「unit 的 ActiveEnterTimestamp 变了(=新实例,排除旧实例没死透)」+ `GET /healthz 200`)
→ 启动自检 banner(抓不到会明说抓不到,不再静默)→ 常驻模型加载进度(`/health` 的
startup 字段;MOSS ASR / vLLM 权重要几分钟,报「加载中」而不假称已就绪)→ 整栈 status。
单元没回到 active 时不印 banner,改印各自日志尾巴 + `enginectl logs <u>` 指路。
等待上限默认 180s(停这一半可能很久:backend 是 TimeoutStopSec=120 顺序 unload vLLM),
`ENGINECTL_WAIT=<秒>` 可覆盖。
底层是 `nous-engine.target`(总闸,`Wants=` 四个 unit:postgresql / backend / status / netprobe)。

## 验证

```bash
enginectl status                        # 推荐:一屏看全栈
systemctl status nous-engine-backend nous-engine-status
journalctl -u nous-engine-backend -f         # 实时日志
```

## 修改后重启

改了 `backend/.env` 或 service 文件 → 重新加载 + 重启。**推荐 `enginectl restart`**(内部用
`--no-block`,不挂终端 + 起完打 banner);或手动:

```bash
sudo systemctl daemon-reload                       # 仅在改了 .service 文件后才需要
enginectl restart                                    # 推荐:不挂终端 + 显示自检 banner
# 或手动(注意加 --no-block,否则阻塞客户端在本机会傻等 job-done 不返回):
sudo systemctl --no-block restart nous-engine-backend
systemctl is-active nous-engine-backend                   # 确认起来了
```

> ⚠️ **别用裸 `sudo systemctl restart nous-engine-backend`**:实测它的阻塞客户端收不到 job-done
> 信号、傻等不返回(挂了 48min,但后端 ~3s 就重启完、active)。`--no-block` 入队即返回,
> 重启照常发生。`enginectl restart` 已封装好。

## 卸载

```bash
sudo ./infra/systemd/install.sh uninstall
```

## 设计选择

- **没把 `vite dev` 服务化** — vite dev server 仅给本机开发用，生产路径走
  backend 直 serve `frontend/dist`（PR #32），不需要常驻。
- **公网隧道已退役(2026-09-06)** — 原 `nous-engine-cloudflared`(`Requires`+`PartOf` backend、
  `--protocol http2`、探针自愈、免密 sudoers)整套删除。历史原因见 git log;不要复活。
- **`MemoryHigh=88G` + `MemoryMax=96G`** on backend — vLLM 之类有 OOM 史，给 host
  留余地。早期是 8G，但 V1' Lane A 之后 wikeeyang fp8mixed 的 dequant 中间态会冲到
  ~18G，8G 上限会让加载在没有 journal 输出的情况下被 SIGKILL。后来 64G —— 但
  Ideogram-4 bf16 整装整模型(54G,from_pretrained + stream pin)峰值 ~76G，64G 会在
  加载中途把整个后端 SIGKILL（2026-06-13 活机 e2e 坐实，连带 vLLM 被一起带走）。
  主机是 125G RAM，96G 能装下该峰值且仍留 ~29G 给 OS + vLLM host 侧。
  单文件 fp8 路(~18G)不受影响。再大的模型/并发多模型可继续上调，留 host ~25G 余地即可。
- **`StartLimitIntervalSec=0` + `RestartSec=15`** on backend — systemd 默认「10s 内
  重启 5 次就彻底放弃」，启动期 OOM/瞬时崩溃环最易触发,触发后服务永久下线、无人知晓。
  单管理员推理机宁可一直重试也不要静默死 → 关掉重启上限,RestartSec=15 拉慢重试防刷屏。
- **`OOMScoreAdjust=-500`** on backend — host 全局 OOM 时让内核优先杀别的(桌面/临时
  工具),保住推理后端(本机核心服务)。注意只影响 host 级「选谁杀」;后端自己超
  `MemoryMax=96G` 时仍由 cgroup OOM 在本 cgroup 内处理,与此无关。
- **`nous-engine-healthprobe.timer`(每 2 分钟)** — 本地健康巡检(`infra/monitoring/
  nous-healthprobe.sh`):探后端本机存活(`/healthz`)、后端自报健康(`/health` 的
  database / load_failures)。结果进
  journal(`journalctl -u nous-engine-healthprobe`)。硬故障(后端连不上 / DB 挂)
  退出非 0 → unit 标 failed,将来接告警只需给探针 service 加 `OnFailure=<alert>.service`。
  **为何要它**:systemd 的 `active` 会骗人,只有真正打一发 HTTP 才看得出来。**不报裸 status==degraded**
  (Lane-K llm runner supervisor 常驻 running:false → 恒 degraded,但 vLLM 独立 spawn、
  LLM 服务正常 → 报它纯噪声)。`NOUS_LOCAL_URL` / `NOUS_PROBE_TIMEOUT` 可覆盖。
- **`nous-engine-status`(独立公开状态监控)** — `infra/monitoring/status_service.py`,**纯 stdlib
  / 独立进程 / 独立端口(127.0.0.1:8001)/ 独立 unit**,刻意不 `Requires`/`PartOf`
  nous-engine-backend —— **后端进程挂了它还活着、显示「后端 API:中断」**(对齐 status.claude.ai
  是独立平台,不是系统模块)。用系统 `/usr/bin/python3` 跑(不碰 backend venv/torch)。自己
  跑 `nvidia-smi` + 读 `/proc` 拿硬件(每卡显存/利用/温度、CPU%、内存、负载、uptime),
  `urllib` 探 `<backend>/health` 拿组件在线/离线。公开无登录,只露硬件概况 + 组件在线/离线,
  不露模型路径/密钥/内部错误。`/`(HTML 自动刷新 15s)、`/api.json`、`/healthz`。
  与 SPA 内 admin 状态页(#547,`/status` 详细版)并存:一个对外独立监控、一个登录后详查。
- **`nous-engine-netprobe`(每进程网络流量,root)** — `infra/monitoring/netprobe.bt` 用 bpftrace
  的 kretprobe 按 pid 累加 TCP/UDP 实际收发字节(只有字节数,没有地址/端口/内容),包装器
  `nous-netprobe.py` 每 2s 原子写 `/run/nous-engine/net_by_pid.json`;后端(heygo)只读它,不提权。
  面板「进程」表的「网络」列与按流量排序来自这里;采集器不在时该列显示「—」。有 bpftrace
  才装(`install.sh` 先 `--dry-run` 附着探针,符号缺失就不启并明说)。spec
  `docs/superpowers/specs/2026-09-06-process-net-traffic-design.md`。

## 日志

systemd 走 journald，不再写 `/tmp/backend.log`。日志自动轮转，磁盘可控。

```bash
journalctl -u nous-engine-backend --since '1 hour ago'
journalctl -u nous-engine-backend -p err                  # 仅 ERROR
journalctl -u nous-engine-backend --vacuum-time=7d         # 仅保留 7 天
```
