"""启动自检 banner —— 一屏聚合关键状态(对齐 PAPERCLIP 的启动面板)。

spec: docs/superpowers/specs/2026-06-18-native-pg-systemd-stack-design.md (PR-3)

设计原则:
- **全 best-effort**:每行独立探测,任一失败显降级值(unknown / UNREACHABLE),
  顶层再包一层 try —— banner 是观测,绝不阻断启动。
- **真实探测**,非硬编码:DB 版本来自实连、GPU 来自 nvidia-smi、bind 来自 argv、
  resident 来自 registry、备份来自 dump 目录 mtime。
- 输出到 stdout(→ journald,`journalctl -u nous-backend` / `nousctl up` 可见);
  仅在 stdout 是 tty(dev-serve 终端)时上色,journald/管道下纯文本。
"""
from __future__ import annotations

import logging
import sys
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_COLOR = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def _mask_db_url(url: str) -> str:
    """postgresql+asyncpg://user:pass@host:port/db → host:port/db(脱敏,不显密码)。"""
    try:
        p = urlparse(url.replace("+asyncpg", "").replace("+psycopg", "").replace("+psycopg2", ""))
        host = p.hostname or "?"
        port = p.port or 5432
        db = (p.path or "/").lstrip("/") or "?"
        return f"{host}:{port}/{db}"
    except Exception:  # noqa: BLE001
        return "?"


async def _db_version(db_url: str) -> str | None:
    """实连目标库取 `select version()` → 'PostgreSQL 17.4'。失败 None。"""
    try:
        from sqlalchemy import text  # noqa: PLC0415
        from src.models.database import create_engine  # noqa: PLC0415

        eng = create_engine()
        try:
            async with eng.connect() as conn:
                v = (await conn.execute(text("select version()"))).scalar()
            return " ".join(str(v).split()[:2]) if v else None
        finally:
            await eng.dispose()
    except Exception:  # noqa: BLE001
        return None


def _bind() -> str:
    """从 uvicorn argv 解析 --host/--port(真实 bind,非硬编码)。"""
    try:
        argv = sys.argv
        host, port = "0.0.0.0", "8000"
        for i, a in enumerate(argv):
            if a == "--host" and i + 1 < len(argv):
                host = argv[i + 1]
            elif a == "--port" and i + 1 < len(argv):
                port = argv[i + 1]
        return f"{host}:{port}"
    except Exception:  # noqa: BLE001
        return "0.0.0.0:8000"


def _gpus() -> list[str]:
    try:
        from src.api.routes.monitor import _gpu_stats_nvidia_smi  # noqa: PLC0415

        out = []
        for g in _gpu_stats_nvidia_smi() or []:
            gb = round(g.get("memory_total_mb", 0) / 1024)
            out.append(f"cuda:{g.get('index')} {g.get('name')} {gb}G")
        return out
    except Exception:  # noqa: BLE001
        return []


def _residents(app) -> list[str]:
    """registry 里标了 resident 的 spec id(配置态,启动时可能还在后台加载)。"""
    try:
        mgr = getattr(app.state, "model_manager", None)
        if mgr is None:
            return []
        return [s.id for s in mgr._registry.specs if getattr(s, "resident", False)]
    except Exception:  # noqa: BLE001
        return []


# 推理栈的关键模块(inference extra)。全在 backend/.venv,裸 uv sync 会裁掉
# → 每次加载才静默 ModuleNotFoundError(2026-07-06 事故)。开机预检,缺了大声报。
_STACK_MODULES = ("vllm", "torch", "safetensors", "transformers")


def _inference_stack(find_spec=None) -> tuple[str, list[str]]:
    """返回 (banner 显示串, 缺失模块列表)。find_spec 只定位不 import → 不触发 CUDA init。

    find_spec 可注入便于测试;默认用 importlib.util.find_spec。
    """
    if find_spec is None:
        import importlib.util  # noqa: PLC0415
        find_spec = importlib.util.find_spec

    missing = []
    for m in _STACK_MODULES:
        try:
            if find_spec(m) is None:
                missing.append(m)
        except (ImportError, ValueError):
            missing.append(m)
    if not missing:
        return f"ok ({'/'.join(_STACK_MODULES[:3])}…)", []
    return (
        _c("1;33", f"⚠ MISSING: {', '.join(missing)} — run: uv sync --all-extras"),
        missing,
    )


def _backup_status() -> str:
    """PR-4 的自动备份落 dump 到 NOUS_DB_BACKUP_DIR;此前显示未配置。"""
    try:
        import datetime  # noqa: PLC0415
        import glob  # noqa: PLC0415
        import os  # noqa: PLC0415

        d = os.environ.get("NOUS_DB_BACKUP_DIR", "")
        if d and os.path.isdir(d):
            files = sorted(glob.glob(os.path.join(d, "*.dump")))
            if files:
                ts = datetime.datetime.fromtimestamp(os.path.getmtime(files[-1])).strftime("%m-%d %H:%M")
                return f"enabled · last {ts} · {len(files)} dumps → {d}"
        return "未配置 (PR-4 待做)"
    except Exception:  # noqa: BLE001
        return "unknown"


async def log_startup_banner(app) -> None:
    """聚合一屏自检 → stdout。绝不抛(banner 是观测,不是门禁)。"""
    try:
        from src.config import get_settings  # noqa: PLC0415

        settings = get_settings()
        version = getattr(app, "version", "?")

        gate = []
        if settings.ADMIN_PASSWORD:
            gate.append("cookie")
        if settings.ADMIN_TOKEN:
            gate.append("bearer")
        auth = "ENABLED (" + "+".join(gate) + ")" if gate else "DISABLED (dev — 无门禁!)"

        dbv = await _db_version(settings.DATABASE_URL)
        db_disp = _mask_db_url(settings.DATABASE_URL)
        db_line = f"connected · {dbv} @ {db_disp}" if dbv else _c("33", f"UNREACHABLE @ {db_disp}")

        gpus = _gpus()
        gpu_line = f"{len(gpus)} · " + " · ".join(gpus) if gpus else _c("33", "none detected")

        residents = _residents(app)
        res_line = " · ".join(residents) if residents else "none"

        stack_line, stack_missing = _inference_stack()
        if stack_missing:
            # banner 可能被后续日志刷走 → 另记一条 WARNING,journald 里可 grep。
            logger.warning(
                "启动预检:推理/图像栈缺失 %s —— venv 可能被裸 `uv sync` 裁过,"
                "跑 `uv sync --all-extras` 修复(否则模型加载会 ModuleNotFoundError)。",
                stack_missing,
            )

        rp = getattr(settings, "ADMIN_PASSKEY_RP_ID", "localhost")
        public = rp if rp and rp != "localhost" else None

        rows = [("Mode", "production · serving frontend/dist"), ("Bind", _bind()), ("Auth", auth)]
        if public:
            rows.append(("Public", public))
        rows += [
            ("Database", db_line),
            ("GPUs", gpu_line),
            ("Resident", res_line),
            ("Inference", stack_line),
            ("DB Backup", _backup_status()),
            ("Logs", "journalctl -u nous-backend -f"),
        ]

        title = f" NOUS-CENTER v{version} "
        rule = "═" * 60
        lines = ["", _c("1;36", "═══" + title + rule[len(title) + 3 :])]
        for k, v in rows:
            lines.append(f"  {_c('2', k.ljust(11))}{v}")
        lines.append(_c("1;36", rule))
        lines.append("")
        print("\n".join(lines), flush=True)
    except Exception:  # noqa: BLE001 — banner 绝不阻断启动
        logger.warning("startup banner 渲染失败(忽略)", exc_info=True)
