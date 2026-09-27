# 迁移 runbook:nous-center → nous-engine

本仓库(推理算力层)改名 **nous-center → nous-engine**,把裸 `nous-` 前缀让给上层新平台
(mediahub 改名 nous)。变更面:

| 维度 | 旧 | 新 |
|---|---|---|
| 仓库目录 | `…/repos/nous-center` | `…/repos/nous-engine` |
| systemd 单元 | `nous-backend` / `nous-cloudflared` / `nous-status` / `nous-dbbackup.*` / `nous-healthprobe.*` / `nous.target` / `nous-aligner`(退役) / `nous-moss-asr`(退役) | 同名加 `-engine`:`nous-engine-backend` … `nous-engine.target` |
| GPU 守护(单独装) | `nous-gpu-guard.service` | `nous-engine-gpu-guard.service` |
| CLI | `nousctl` | `enginectl` |
| sudoers drop-in | `/etc/sudoers.d/nous-deploy`、`nous-healthprobe` | `nous-engine-deploy`、`nous-engine-healthprobe` |
| GitHub remote | `iocrazy/nous-center` | `iocrazy/nous-engine`(网页改名 + set-url) |

**不改的东西**(刻意保留):

- **cloudflared 隧道名** `nous-center`(`cloudflared tunnel run/route dns nous-center`)—— 是
  Cloudflare 侧的隧道标识,不是仓库/单元名。除非在 Cloudflare 重建隧道,否则保持原名。
- **隧道域名** `api.iocrazy.com`、DB 备份目录 `nous-db-dumps`、cookie `nous_admin_session`、
  secret-at-rest HKDF info、`NOUS_CENTER_HOME`/TOTP issuer 等**功能性串**(动了会掉登录/掉密钥/
  掉备份)——本包不碰。
- **backend/frontend 源码里的品牌字样**(API `owned_by`、passkey RP、状态页/前端标题等)——见文末
  「可选项」,归品牌 rebrand,与本 infra 包解耦、可后做。
- **`nous-prod` 生产检出目录名 / `.nous-production` 标记** —— 是「生产 vs dev 检出分离」的独立概念,
  非本次改名对象。

---

## ⚠️ 迁移前必读

- **有可视化停机窗口**:迁移会 stop 后端 + 隧道再迁目录重装,公网 `api.iocrazy.com` 会有
  **数分钟不可用**(外部平台若正调 ASR/推理会中断)。挑低峰、提前告知依赖方。
- 迁移**在生产机本机**跑(要 root、要碰 `/etc/systemd/system`、要 mv 大盘目录)。
- 迁移脚本会 `mv` 掉仓库目录本身 —— 全逻辑包在 `main()`、末行才 `main "$@"`,避免自吃尾巴。
- **GitHub 改名由你在网页操作**(脚本不碰 remote);脚本只在收尾打印 `set-url` 命令。

### 前置检查清单

- [ ] 当前分支 `feat/rename-nous-engine` 已 **merge 进 master 并在生产检出 checkout**
      (脚本会校验 `infra/systemd/nous-engine-backend.service` 存在,缺则拒绝)。
- [ ] 记一份**当前 enabled 状态**(脚本也会自动记录并按原状恢复,此处人工留底):
      ```bash
      for u in nous-backend nous-cloudflared nous-status nous-dbbackup.timer \
               nous-healthprobe.timer nous.target nous-aligner; do
        printf '%-26s %s\n' "$u" "$(systemctl is-enabled "$u" 2>/dev/null || echo n/a)"
      done
      ```
      实测基线:`nous-backend`/`nous-status`/`nous.target`/两个 `.timer` = **enabled**;
      `nous-cloudflared` = **disabled**(由 target 拉起,隧道暂缓);`nous-aligner` = **disabled**;
      `nous-moss-asr`/`nous-gpu-guard` = **未安装**。
- [ ] 确认管理员 drop-in `/etc/systemd/system/nous-healthprobe.service.d/`(如
      `defer-tunnel.conf`: `NOUS_TUNNEL_AUTOHEAL=0`)—— 脚本会自动搬到新单元名下,人工核对即可。
- [ ] 备好 cloudflared 二进制路径(若 `/usr/local/bin/cloudflared` 当前缺失),迁移时用
      `--cloudflared <path>` 传入;不传且缺失则脚本告警继续(不阻塞)。
- [ ] 数据库无需动(同一 Postgres,`DATABASE_URL` 不变);稳妥起见迁移前可手跑一次
      `sudo systemctl start nous-dbbackup.service` 留一份 dump。

---

## 执行

```bash
# 生产检出内(改名分支已在 master、已 checkout)
cd /media/heygo/program/projects-code/repos/nous-center

# 先干跑核对动作(不落盘)
sudo ./infra/migrate-to-nous-engine.sh --dry-run

# 正式迁移(缺 cloudflared 二进制时带 --cloudflared 指向它,示例路径按实际改)
sudo ./infra/migrate-to-nous-engine.sh --cloudflared /media/heygo/Program/bin/cloudflared
# 若 /usr/local/bin/cloudflared 已在,可省 --cloudflared:
#   sudo ./infra/migrate-to-nous-engine.sh
```

脚本步骤:① 前置检查 → ② 记录 enabled 状态 → ③ 停旧单元 → ④ 搬 drop-in + 删旧 unit/nousctl/旧
sudoers → ⑤ `mv` 仓库目录 → ⑥ 装新 unit + `enginectl` + sudoers + daemon-reload + 按②恢复
enable → ⑥b 恢复 enable → ⑦ cloudflared 二进制修复 → ⑧ 迁 Claude 记忆目录 → ⑨ healthz 轮询 +
逐单元 is-active → ⑩ 摘要 + 回滚提示。任何一步失败即打印位置退出,不半途静默。

> `nous-gpu-guard` / `nous-moss-asr` 未在脚本纳管(前者单独由 `infra/gpu/setup-gpu-mitigations.sh`
> 装、当前未上机;后者已退役由 ModelManager 起)。若 gpu-guard 曾装过,迁移后手动重装:
> `sudo ./infra/gpu/setup-gpu-mitigations.sh`(新单元名 `nous-engine-gpu-guard`)。

---

## 验证清单(全 200/active 才算成)

```bash
enginectl status                                   # 一屏:各 unit active + 端口 + 公网隧道
systemctl is-active nous-engine-backend nous-engine-status nous-engine.target
curl -s --noproxy '*' -m5 http://127.0.0.1:8000/healthz         # 本机 200
curl -s -m8 https://api.iocrazy.com/healthz                     # 公网 530→200(隧道回来)
```

- [ ] `nous-engine-backend` active、本机 `/healthz` 200。
- [ ] 引擎已 loaded:`curl -s http://127.0.0.1:8000/api/v1/engines`(或 UI 任务中心不再卡「加载 0/N」)。
- [ ] 公网 `https://api.iocrazy.com/healthz` 从 530(隧道刚重启)恢复到 200(~1 分钟内;
      healthprobe 自愈会兜)。
- [ ] `enginectl logs backend` 有正常启动 banner;`journalctl -u nous-engine-healthprobe` 每 2 分钟 OK。
- [ ] drop-in 生效:`systemctl show nous-engine-healthprobe -p Environment` 含 `NOUS_TUNNEL_AUTOHEAL=0`
      (若原来有)。
- [ ] 免密自愈/发版仍工作:`sudo -n systemctl restart nous-engine-cloudflared`(healthprobe 用)、
      `deploy.sh` 的 `restart nous-engine-backend` 都命中新 sudoers。

---

## 回滚

健康检查未过且需还原(脚本 ⑩ 也会打印同款):

```bash
# 1. 拆新单元
sudo systemctl disable --now nous-engine-backend nous-engine-status nous-engine.target \
     nous-engine-healthprobe.timer nous-engine-dbbackup.timer 2>/dev/null
sudo rm -f /etc/systemd/system/nous-engine-* /usr/local/bin/enginectl /etc/sudoers.d/nous-engine-*
# 2. 目录搬回
sudo mv /media/heygo/program/projects-code/repos/nous-engine \
        /media/heygo/program/projects-code/repos/nous-center
# 3. 从 git 历史恢复旧 unit 文件(改名前的 master)
cd /media/heygo/program/projects-code/repos/nous-center
git checkout master -- infra/        # 或指定改名前的 commit
# 4. 用旧 install.sh 重装 nous-* 单元
sudo ./infra/systemd/install.sh
# 5. (若迁过)记忆目录搬回
sudo -u heygo mv ~heygo/.claude/projects/-media-heygo-program-projects-code-repos-nous-engine \
                 ~heygo/.claude/projects/-media-heygo-program-projects-code-repos-nous-center
```

---

## GitHub 收尾(网页操作 + 一条命令)

1. GitHub 网页:`iocrazy/nous-center` → Settings → Rename repository → `nous-engine`。
   (GitHub 会自动 301 旧 URL 一段时间,但仍应更新 remote。)
2. 更新本地 remote:
   ```bash
   git -C /media/heygo/program/projects-code/repos/nous-engine \
       remote set-url origin https://github.com/iocrazy/nous-engine.git
   git -C …/nous-engine remote -v     # 确认
   ```
3. 若有 `nous-prod` 生产 worktree / 其它检出,各自 `remote set-url` 同步。
4. CI / self-hosted runner(`actions.runner.iocrazy-nous-app.*`)若按仓库名注册,按需在
   GitHub Actions 设置里核对(runner 注册名不随仓库改名自动变)。

---

## 可选项:品牌 rebrand(与本 infra 包解耦,可后做)

> **2026-09-27 已完成**(chore/rebrand-nous-engine):下列展示串已改为 nous-engine;「务必不动的功能串」
> 与 `backend/pyproject.toml` 包名(会连带重生成 uv.lock)、前端 localStorage key `nous-center:workspace`
> (改了丢工作区状态)、DB 名 `nous_center` 刻意保留。

本包**只做 infra/路径/单元改名**,不动 backend/frontend 源码的品牌字样。以下是纯展示品牌,
改动需重 build 前端 + 跑对应后端测试,单独一批走:

- 前端 UI 标题/文案:`frontend/index.html`、`frontend/src/**` 里的 `nous-center`(网页标题、登录页等)。
- 状态页公开标题:`infra/monitoring/status_service.py` 的 `<title>` / `<h1>`「nous-center 系统状态」。
- API 展示串:`openai_compat.py` 的 `owned_by="nous-center"`、node.yaml 的 `author` 归属等。
- **务必不动的功能串**(会破坏运行,不属品牌 rebrand):`secret_crypto.py` 的 HKDF info、
  `config.py` 的 `NOUS_CENTER_HOME`/`ADMIN_PASSKEY_RP_NAME`、`admin_totp.py` 的 TOTP issuer、
  cookie `nous_admin_session`、DB 备份目录 `nous-db-dumps`、cloudflared 隧道名 `nous-center`。
