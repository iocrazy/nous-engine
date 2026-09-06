# Admin secrets

nous-engine 有**两套**独立的 admin 鉴权，并行存在：

| 用途 | env | 鉴权位置 | 谁用 |
|---|---|---|---|
| **浏览器登录** | `ADMIN_PASSWORD` + `ADMIN_SESSION_SECRET` | `httponly` cookie，HMAC 签名 | UI（`/sys/admin/login` → `Set-Cookie: nous_admin_session=...`） |
| **CLI / curl** | `ADMIN_TOKEN` | `Authorization: Bearer <token>` header | 脚本、curl、CI 任务 |

两套互不干扰：UI 走 cookie，外部脚本走 header。

## 一键生成

```bash
./infra/security/gen-admin-secrets.sh
```

打印到 stdout 三个值。**不会**自动写 `.env` — 你应该看一眼再贴：

```bash
./infra/security/gen-admin-secrets.sh >> backend/.env
```

或者手动 copy。

## 用 CLI token 调 admin API

```bash
TOKEN="<ADMIN_TOKEN value>"

# 重启 model registry（admin-only）
curl -H "Authorization: Bearer $TOKEN" -X POST http://127.0.0.1:8000/api/v1/engines/reload

# 列 API keys
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/keys
```

注意：

- **API endpoint 路径是 `/api/v1/keys`** （不带 `api-` 前缀）
- **UI 路由路径是 `/api-keys`** （连字符）— 这是用户在浏览器地址栏看到的，不是后端 API
- 别混了 — 调 backend 永远用 `/api/v1/keys`

## 轮换密码

```bash
# 1. 生成新值
./infra/security/gen-admin-secrets.sh

# 2. 编辑 backend/.env 替换旧值

# 3. 重启 backend（systemd 用户）
sudo systemctl restart nous-engine-backend

# 4. 所有现存浏览器 session 立即失效（HMAC secret 变了）— 用户需要重新登录
```

## 为什么不存 bcrypt

当前实现用 `hmac.compare_digest` 直接比明文密码（密码本身在 .env 明文存）。
对单管理员 + 自托管场景是合理简化：

- `.env` 已经被 `.gitignore` 排除，不进仓库
- `nous-engine` 是单管理员推理 infra，不存其他用户的密码
- bcrypt 主要解决 DB 泄露后批量破解 — 这里没有 DB 存密码

如果未来变多租户 / 有 user 表，改成 bcrypt + per-user salt。
