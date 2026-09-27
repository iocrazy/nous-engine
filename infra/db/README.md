# infra/db — 原生 PostgreSQL 托管 + 迁移

spec: `docs/superpowers/specs/2026-06-18-native-pg-systemd-stack-design.md`

nous-engine 的数据库从 **Docker Desktop** 迁到**原生 pg17 + systemd**。Docker Desktop
在 Linux 上跑在 VM 内、socket 用户态、不接开机链,不适合做常驻数据层。原生 pg 经
`postgresql.service` 天然进 systemd 依赖链(`nous-backend.service` 的 `Requires=/After=`
已指向它)。

## 落盘策略

- **数据**:系统盘默认位置 `/var/lib/postgresql/17/main`(系统盘空间充裕;DB 只含元数据 +
  结构化日志,模型/图都不在库里,体量小)。
- **备份**(PR-4):**NAS** `/mnt/heytime/backup/nous-db-dumps`(CIFS `//192.168.3.27/sources`,
  异机)。备份落异机,本地不留副本 → 整机损坏/格式化时备份不随本机一起丢。NAS 不可达时
  当次备份失败(journal 可见),不静默成功。

## 一次性迁移 runbook

> 迁移期 **docker@5432(源) 与 原生@5433(目标) 双 pg 并存**,跨端口迁;
> 逐表 count 校验通过后,才把原生切回 5432。全程源库只读,可随时回滚。

### 1. 装原生 pg17 + 客户端

```bash
# 若发行版自带源无 pg17,加 PGDG 官方源:
sudo apt install -y postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh   # 交互一次,加 PGDG 源
sudo apt update
sudo apt install -y postgresql-17 postgresql-client-17
```

装完 `postgresql-common` 会自动建 cluster `17-main`。因 **5432 被 docker 占用**,
`pg_createcluster` 会自动顺延端口(通常 5433)。确认:

```bash
pg_lsclusters          # 看 17 main 的端口(迁移期应为 5433)
```

若未自动建在 5433,显式建一个迁移用 cluster:

```bash
sudo pg_createcluster 17 main --port 5433 --start
```

### 2. 安全配置(对齐原 compose:只绑 localhost)

编辑 `/etc/postgresql/17/main/postgresql.conf`:

```
listen_addresses = 'localhost'
```

`/etc/postgresql/17/main/pg_hba.conf` 本机用 `scram-sha-256`(默认即可)。改完:

```bash
sudo pg_ctlcluster 17 main restart
```

### 3. 建 role + 库,迁数据,校验(本目录脚本)

```bash
cd /media/heygo/Program/projects-code/_playground/nous-center
# 密码自动从 backend/.env 的 DATABASE_URL 抠;也可显式 NOUS_DB_PASSWORD=*** 前缀
./infra/db/migrate-docker-to-native.sh all      # precheck→init-target→migrate→verify
```

`verify` 逐表 count 比对源/目标,**任一不一致即退出码 1 中止**,docker 源库原封不动。
全绿才继续。

### 4. 切端口:原生 5433 → 5432(停 docker pg 后)

```bash
# a. 停 docker pg,释放 5432(数据是 bind-mount 目录,容器删了数据还在)
cd /media/heygo/Program/Docker/Containers/datahub/nous-center
docker compose down

# b. 原生 cluster 改 5432
sudo sed -i 's/^port = 5433/port = 5432/' /etc/postgresql/17/main/postgresql.conf
sudo pg_ctlcluster 17 main restart
pg_lsclusters                                   # 确认 17 main → 5432
sudo systemctl enable postgresql                # 开机自启

# c. 验证后端连得上(连接串 localhost:5432/nous_center 不用改)
pg_isready -h 127.0.0.1 -p 5432
```

### 5. 收尾

- `backend/.env` 的 `DATABASE_URL` **不用改**(仍 `localhost:5432/nous_center`)。
- Docker compose 目录 + `postgre-data` **保留数日做冷备**,确认稳定后再删。
- n8n 的库 `nous_database` 在另一容器,本迁移**不碰**。

## 回滚

迁移期/切换后数日内出问题:

```bash
sudo pg_ctlcluster 17 main stop                 # 让出 5432
cd /media/heygo/Program/Docker/Containers/datahub/nous-center
docker compose up -d                            # docker pg 回到 5432,数据是未被触碰的 bind-mount
```

`DATABASE_URL` 不变,后端重连即恢复。

## 自动备份(PR-4)

每日 `pg_dump` 自定义格式 → NAS 备份目录(异机,本地不留副本),滚动保留 N 天,
落盘后校验 `pg_restore --list` 可读才原子改名(坏 dump 不冒充好备份)。

- 脚本 `backup.sh`(env:`NOUS_DB_BACKUP_DIR` 默认 `/mnt/heytime/backup/nous-db-dumps`、
  `NOUS_DB_BACKUP_KEEP_DAYS` 默认 14)。
- systemd:`nous-dbbackup.timer`(每日 03:30,`Persistent` 补跑)触发 oneshot
  `nous-dbbackup.service`。装机 `install.sh` 自动 enable。
- 手动即时备份:`./infra/db/backup.sh`。
- 启动 banner 的「DB Backup」行读 `NOUS_DB_BACKUP_DIR` 最新 dump mtime —— 在
  `backend/.env` 设 `NOUS_DB_BACKUP_DIR` 后 banner 才显示备份状态。
- 从备份恢复:`pg_restore --clean --if-exists -h 127.0.0.1 -p 5432 -U nous_heygo -d nous_center <dump>`。

## 脚本

- `migrate-docker-to-native.sh` — `precheck|init-target|migrate|verify|all`。幂等、
  逐表校验、不一致中止。详见脚本头注释。
- `backup.sh` — 每日备份(timer 触发或手动)。落盘后校验 + 滚动保留。
