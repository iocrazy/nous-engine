# AGENTS.md — 给 Codex 等非 Claude 的 coding agent

项目知识(架构、GPU 放置、测试约定、部署流程)统一写在 [`CLAUDE.md`](CLAUDE.md),动手前先读它。
本文件只列**硬规矩**。

## 这个目录是生产检出

`/media/heygo/program/projects-code/repos/nous-engine` 就是 systemd 直接运行的那份代码:
- 后端 `nous-engine-backend.service`(:8000),serve `frontend/dist/`
- ComfyUI sidecar `nous-engine-comfyui.service`(:8888,钉 Pro 6000,地址见 `infra/network.env`)

维护与上线由 Claude Code/Codex 会话负责,走 PR → CI → 合并 → `infra/deploy.sh`。

## 禁止

- kill / pkill / killall 后端、vLLM、sgl-omni、ComfyUI 的任何进程
- `systemctl restart|stop|start|edit` 任何 `nous-engine-*` 单元;`enginectl`;`daemon-reload`
- 改 `/etc/systemd/system/nous-engine-*.service`
- 自己另起一个 ComfyUI 去占 8888
- **直接改生产工作树里的任何文件**(包括新建);仅允许通过 `git worktree add`
  在已忽略的 `.worktrees/` 下创建独立工作树

## 要改代码

```bash
git fetch origin
git worktree add .worktrees/<topic> -b <branch> origin/master
# 在 .worktrees/<topic> 里改、跑测试,然后 push 分支、开 PR
```

测试约定见 `CLAUDE.md` 的 Testing 一节:只跑 PostgreSQL、`uv run pytest tests -n 8`、
**绝不裸 `uv sync`**、测试绝不能真起推理服务。

## 需要重启 / 部署 / 换常驻模型 / 改 ComfyUI 配置

**执行前停下来告诉用户；得到明确授权后，Codex 可按规定部署流程操作。**
没有 sudo 不是改用 kill 的理由，也不得绕过 `infra/deploy.sh` 直接操作 systemd。

## 为什么有这份文件

2026-09-25:有 agent 在 `systemctl` 没权限后直接 kill 了 systemd 的 ComfyUI,另起一个只绑
127.0.0.1 的实例 → 从 Tailscale 访问 ComfyUI 中断一天多。
2026-09-26:有 agent 把未评审的新路由直接写进本目录,再 `kill -TERM` 后端让它生效 →
生产跑上未过 CI 的代码,同时打断了正在进行的常驻模型切换。
