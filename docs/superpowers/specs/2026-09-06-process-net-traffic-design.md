# 进程表加网络流量列 + 可排序 — 设计

日期:2026-09-06 · 状态:待用户审阅 · 范围:管理台「系统状态 → 进程」表

## 0. 一句话

给系统状态页的进程表加一列「网络」(每进程 TCP/UDP 收发速率,**覆盖所有进程含 docker/root**),
并让 CPU% / MEM / 网络 三列可点击排序。数据来自一个新的 root 采集器(bpftrace),经文件交给后端。

## 1. 背景与动机

- 用户刚把 nous-engine 的公网隧道退役(PR #719),想在面板上直接看到「哪个进程在往外走流量」。
- Linux 没有每进程网络计数器:`/proc/<pid>/io` 是磁盘+套接字混算且只能读本用户;`ss -tinp`
  只能归属本用户进程且只看在线连接。**用户拍板要覆盖 docker/root**,唯一干净的路是内核
  探针(eBPF),而 eBPF 需要 root —— 后端以 `heygo` 跑,不给它提权,单独起一个采集器。
- 现状:`backend/src/api/routes/monitor.py::_top_processes` 用 psutil 取 CPU/内存前 20,固定按
  CPU 排;前端 `DashboardOverlay.tsx` 渲染前 15 行,表头不可点。

## 2. 组件

### 2.1 采集器 `nous-engine-netprobe`(新,root systemd 单元)

- 文件:`infra/monitoring/netprobe.bt`(bpftrace 脚本)+ `infra/monitoring/nous-netprobe.py`
  (纯 stdlib 包装器,系统 `/usr/bin/python3`)+ `infra/systemd/nous-engine-netprobe.service`。
- 探针(只累加字节,不抓包、不看内容):
  - `kprobe:tcp_sendmsg` → `@tx[pid] += arg2`
  - `kprobe:tcp_cleanup_rbuf` → `@rx[pid] += arg1`(内核把数据拷给用户态时的 copied)
  - `kprobe:udp_sendmsg` / `kprobe:udpv6_sendmsg` → `@tx[pid] += arg2`
  - `kretprobe:udp_recvmsg` / `kretprobe:udpv6_recvmsg` → `retval > 0` 时 `@rx[pid] += retval`
  - `interval:s:2` → `print(@tx); print(@rx); clear(@tx); clear(@rx);`
- 包装器:`bpftrace -f json netprobe.bt` 的 stdout 逐行读,每个 interval 合成一份
  `{"ts": <epoch>, "interval_s": 2, "pids": {"<pid>": {"tx": <bytes>, "rx": <bytes>}}}`,
  **写临时文件再 rename** 到 `/run/nous-engine/net_by_pid.json`(原子,后端永远读到完整 JSON)。
  bpftrace 退出 → 包装器退出非 0 → systemd `Restart=on-failure` 拉起。
- 单元:`User=root`、`RuntimeDirectory=nous-engine`(`/run/nous-engine`,0755)、
  `ProtectSystem=strict` + `ProtectHome=yes`(只需要 /run 与 tracefs)、`MemoryMax=512M`、
  `Restart=on-failure` `RestartSec=5`、`WantedBy=multi-user.target`,并进 `nous-engine.target` 的 `Wants`。
- 覆盖 docker/root 的原因:容器进程在宿主内核里就是宿主 pid,kprobe 按 pid 计数天然覆盖;
  后端 psutil 那份 CPU/内存本来就含所有进程(读 `/proc/<pid>/stat` 不受用户限制),按 pid 能对上。
- 安装:`install.sh` 里 `command -v bpftrace` 有才装+启,没有就 `warn` 跳过(后端与面板照常,
  只是该列显示「—」)。安装自检:`bpftrace --dry-run netprobe.bt` 附着一次全部探针,失败 `bad`
  并给出缺哪个符号。

### 2.2 后端(改 `monitor.py`)

- 新增 `_read_net_by_pid(path=NET_BY_PID_PATH, max_age_s=10) -> tuple[dict[int, tuple[int,int]], dict]`:
  读文件、`ts` 距现在 ≤ 10s 才算有效;缺失 / 过期 / 坏 JSON 一律返回 `({}, {"available": False, "age_s": None|age})`,
  不抛、不打 warning 风暴(每次轮询都会调,只在状态**翻转**时 info 一行)。路径常量
  `NET_BY_PID_PATH = os.environ.get("NOUS_NET_BY_PID", "/run/nous-engine/net_by_pid.json")`。
- `_top_processes(limit=20, net=None)`:每行加 `net_tx_bps` / `net_rx_bps`(`bytes / interval_s`,
  int;采集器不可用 → `None`)。列表 = CPU 前 20 **∪** 有流量(tx+rx>0)但不在前 20 的 pid
  (按流量降序补,总上限 40)。补进来的 pid 也经 psutil 取 name/cmdline/mem(取不到就跳过)。
- `/api/v1/monitor/stats` 响应新增顶层 `net_probe: {"available": bool, "age_s": float|None}`。
- `_top_processes` 仍在 `asyncio.to_thread` 里跑;读文件也在同一线程里。

### 2.3 前端(改 `DashboardOverlay.tsx` + `api/system.ts` 类型)

- 类型:`ProcessRow` 加 `net_tx_bps: number | null`、`net_rx_bps: number | null`;
  `MonitorStats` 加 `net_probe`。
- 表头 CPU% / MEM / 网络 三列可点击:点击设排序键,再点同列切升降;默认 `cpu desc`。
  排序状态放组件 `useState`(不持久化)。排序后取前 15 行。
- 「网络」列:`▲ 12.3 KB/s ▼ 0.8 KB/s`(`formatRate`:<1KB 显示 B/s,<1MB 显示 KB/s 一位小数,
  否则 MB/s);`null` → `—`。`net_probe.available === false` 时表头带 tooltip
  「采集器未运行:sudo ./infra/systemd/install.sh」。
- 按网络排序时键值 = `(tx ?? 0) + (rx ?? 0)`。

## 3. 错误处理

| 情形 | 行为 |
|---|---|
| bpftrace 不存在 | install.sh warn 跳过;后端 `available: false`;列显示「—」 |
| 探针符号在当前内核不存在 | `--dry-run` 自检 `bad` + 列出失败探针;单元不启 |
| 采集器崩溃/重启中 | 文件过期 → 10s 后 `available: false`,列「—」;恢复自动回来 |
| 文件被截断/半写 | 不可能(rename 原子);坏 JSON 仍按不可用处理 |
| pid 在两次采样间退出 | 该 pid 有流量无 psutil 信息 → 跳过 |

## 4. 安全边界

- 采集器只累加字节数,不记录地址/端口/内容;文件只有 pid→字节。
- 后端不提权;采集器 `ProtectSystem=strict`,只写 `/run/nous-engine`。
- 前端排序纯客户端,不新增端点、不新增权限。

## 5. 测试

- 后端 `tests/test_monitor_net_probe.py`:① 新鲜文件 → bps 正确、`available: true`;② 过期(ts 老 30s)→
  全 `None`、`available: false, age_s≈30`;③ 缺失 / 坏 JSON → 同②且不抛;④ 有流量但不在 CPU 前 20 的
  pid 被补进来且总数 ≤ 40(psutil 用 monkeypatch 假进程)。路径经 `NOUS_NET_BY_PID` 指到 tmp。
- 前端 vitest:`sortProcesses(rows, key, dir)` 纯函数三列各一例 + `formatRate` 边界。
- 真机(非 CI):`sudo bpftrace --dry-run infra/monitoring/netprobe.bt` 通过;`enginectl status`
  多一行 netprobe;面板「网络」列在跑一次 `curl -o /dev/null https://hf-mirror.com/...` 时对应
  curl 进程出现 ▼ 速率。

## 6. 不做(YAGNI)

历史曲线 / 按连接或域名拆分 / 告警 / 单进程详情 / 磁盘 I/O 列。

## 7. 前置(用户敲一次 sudo)

内核 7.0 上探针符号是否齐:
`sudo bpftrace -l 'kprobe:tcp_sendmsg' 'kprobe:tcp_cleanup_rbuf' 'kprobe:udp_sendmsg' 'kprobe:udp_recvmsg' 'kprobe:udpv6_sendmsg' 'kprobe:udpv6_recvmsg'`
六个都列出来才按本文实施;缺哪个,在计划里换成对应 tracepoint(`tracepoint:sock:*` /
`tracepoint:tcp:*`)再实施。

## 8. 顺手清理

`install.sh` 删除 `/etc/systemd/system/nous-engine-healthprobe.service.d/`(里面只剩已失效的
`NOUS_TUNNEL_AUTOHEAL=0`,隧道退役后无意义)。
