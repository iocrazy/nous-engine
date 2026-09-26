import os

# CRITICAL SAFETY: disable lifespan background tasks BEFORE any app import.
# The default set includes memory_guard_loop which polls nvidia-smi via
# subprocess every 5s. When test files using starlette/fastapi TestClient
# trigger lifespan (test_ws_tts.py, test_api_errors.py), concurrent
# nvidia-smi calls can crash the NVIDIA driver → X session logout. This
# env var (consumed in src/api/main.py lifespan) gates all asyncio.create_task
# calls so tests never spawn those loops.
os.environ.setdefault("NOUS_DISABLE_BG_TASKS", "1")

# CRITICAL SAFETY: hide CUDA so any torch/vllm import sees zero devices,
# preventing libcudart dlopen / CUDA context init during tests.
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["NVIDIA_VISIBLE_DEVICES"] = ""

# CRITICAL SAFETY (2026-07-05 事故): pytest 把 SIGTERM 广播到宿主 mihomo/sshd/
# systemd user@1000 —— 清理代码 `os.killpg(os.getpgid(pid), sig)` 在 pid 是孤儿/
# 已死回收时解析出 pgid<=1,killpg(1)==kill(-1) 向该用户所有进程广播。生产代码已
# 全部改走 src.services.safe_signal;这里再给测试进程一道**物理护栏**:模块级替换
# os.killpg/os.kill,pgid<=1 / pid<=1 一律 raise(把 bug 就地炸出来,绝不投递),
# pgid>1 / pid>1 才委托真实实现(自己 spawn 的子进程仍能正常清理)。装在模块级
# 而非 fixture,连 collection 期 / lifespan 期直接调 os.killpg 的路径也覆盖。
_real_killpg = os.killpg
_real_kill = os.kill


def _guarded_killpg(pgid, sig):
    assert pgid > 1, (
        f"BLOCKED broadcast killpg(pgid={pgid}, sig={sig}) in tests — "
        "pgid<=1 would signal every process the user owns (host takedown bug)."
    )
    return _real_killpg(pgid, sig)


def _guarded_kill(pid, sig):
    assert pid > 1, (
        f"BLOCKED kill(pid={pid}, sig={sig}) in tests — pid<=1/0 broadcasts "
        "to init / caller's process group (host takedown bug)."
    )
    return _real_kill(pid, sig)


def _is_own_descendant(pid: int) -> bool | None:
    """pid 是不是本测试进程的后代。/proc 里查不到(已死/不存在)→ None。

    沿 /proc/<pid>/stat 的 ppid 链往上走到 1 为止。`start_new_session=True` 只换
    session/pgid,不换 ppid,所以自己 spawn 的 vLLM 式子进程照样算后代。
    """
    me = os.getpid()
    cur, seen = pid, 0
    while cur > 1 and seen < 64:
        if cur == me:
            return True
        try:
            with open(f"/proc/{cur}/stat") as f:
                # comm 字段可能含空格/括号,取最后一个 ')' 之后再切
                cur = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None if seen == 0 else False
        seen += 1
    return False


# CRITICAL SAFETY (2026-09-22 事故):测试把**生产**的 vLLM 杀了,持续了一整夜。
# test_components_routes 的 `with TestClient(app)` 跑真 lifespan → 启动期「孤儿 vLLM
# 清扫」扫的是**真机 /proc** → 看见生产后端起的 vLLM:embedding 那个对不上 llm spec
# 判 kill_unmatched 当场 SIGKILL,LLM 那个被测试进程「接管」、lifespan 关闭时一并收掉。
# 上面的 pid<=1 护栏拦不住 —— 那是真实的 pid>1。生产侧表现是每跑一次全量测试,
# 常驻模型就「端口不可达 → 看门狗自愈」一轮,kill 日志落在 pytest 输出里、不在后端
# journal,所以查了很久只看到退出码 -9 看不到是谁。
# 这里是**物理护栏**:测试进程只准给**自己的后代**发真信号(sig 0 探活不算)。
# 不是后代 → 直接炸;查不到的 pid 交给真实实现(它会 ProcessLookupError)。
def _refuse_foreign(pid: int, sig, what: str) -> None:
    if sig == 0 or pid <= 1:
        return  # pid<=1 交给上面的广播护栏,报它自己那条更准确的错
    if _is_own_descendant(pid) is False:
        raise AssertionError(
            f"BLOCKED {what}(pid={pid}, sig={sig}) in tests — pid 不是本测试进程的后代,"
            "很可能是本机生产服务(vLLM/后端)。测试只能 mock 或只杀自己 spawn 的进程。"
        )


def _guarded_killpg_owned(pgid, sig):
    _refuse_foreign(pgid, sig, "killpg")
    return _guarded_killpg(pgid, sig)


def _guarded_kill_owned(pid, sig):
    _refuse_foreign(pid, sig, "kill")
    return _guarded_kill(pid, sig)


os.killpg = _guarded_killpg_owned
os.kill = _guarded_kill_owned
# 另一半在 src/api/main.py:NOUS_DISABLE_ORPHAN_SWEEP=1 时启动期根本不扫真机进程。
os.environ["NOUS_DISABLE_ORPHAN_SWEEP"] = "1"

# CRITICAL SAFETY (2026-09-02 事故): 测试进程里**绝不允许真起推理服务子进程**。
# test_vllm_adapter 里一个用例走到 VLLMAdapter.load() 的真 Popen,而 llm_vllm.py 对
# device="cpu" 会把 CUDA_VISIBLE_DEVICES 设成 "0"(覆盖上面的 ""),于是
# `python -m vllm.entrypoints...` 真的在 GPU 上初始化 CUDA;xdist 8 worker × 两个并发
# 跑批一起起,NVRM 先报 VA 空间耗尽,随后 RTX 3090 GSP RPC 超时(Xid 175/154),
# nvidia-smi 全进 D 态,后端 API、Xwayland、ComfyUI 全冻,只能重启机器。
# CI 用 --ignore 挡住那个文件,本地(生产 venv 装了 vllm)裸跑 `pytest tests` 就中招。
# 这里在 Popen 层面拦:argv 里出现推理服务入口就 raise(把问题就地炸出来),
# 显式 NOUS_RUN_GPU_TESTS=1 才放行(真机 e2e)。子类化保留 isinstance / patch 语义。
import subprocess as _subprocess

_real_popen = _subprocess.Popen
_INFERENCE_SERVER_MARKERS = ("vllm.entrypoints", "sglang.launch_server", "sgl-omni")


class _GuardedPopen(_real_popen):
    def __init__(self, args, *a, **kw):
        argv = args if isinstance(args, (list, tuple)) else [args]
        joined = " ".join(str(x) for x in argv)
        if (
            any(m in joined for m in _INFERENCE_SERVER_MARKERS)
            and os.environ.get("NOUS_RUN_GPU_TESTS") != "1"
        ):
            raise AssertionError(
                "BLOCKED real inference-server spawn in tests: "
                f"{joined[:160]} — mock subprocess.Popen in the test, or set "
                "NOUS_RUN_GPU_TESTS=1 for a deliberate on-GPU e2e run."
            )
        super().__init__(args, *a, **kw)


_subprocess.Popen = _GuardedPopen

# Tests must not inherit the developer's local admin password from .env —
# pydantic-settings reads .env automatically, which would 401 every admin-gated
# route. Force-empty here disables the cookie gate (dev mode) for the whole
# suite. Bare assignment (not setdefault) overrides whatever .env provides.
os.environ["ADMIN_PASSWORD"] = ""
# is_login_required() (admin_session.py) arms the gate on `ADMIN_PASSWORD or
# ADMIN_TOKEN`. Clearing only ADMIN_PASSWORD leaves the gate armed on any box
# whose backend/.env sets ADMIN_TOKEN (every production/deploy host) —
# pydantic-settings reads .env automatically → the whole admin-gated suite 401s
# locally while passing on CI (no .env). Force-empty ADMIN_TOKEN too so the
# dev-mode gate-off is honored everywhere.
os.environ["ADMIN_TOKEN"] = ""
# image_generate node demands a signing secret since p2-polish-3 (no more
# base64 fallback). Tests don't need a real secret — anything non-empty
# unlocks the URL path. Specific tests that exercise the missing-secret
# error monkeypatch this back to "".
os.environ["ADMIN_SESSION_SECRET"] = "tests-only-do-not-use-in-prod"

# Skip mounting the SPA catch-all in tests — its /{full_path:path} would shadow
# routes that test fixtures register on the app after create_app() returns.
os.environ["NOUS_DISABLE_FRONTEND_MOUNT"] = "1"

# Image output storage writes to ~/.gstack/outputs/images by default.
# Redirect to a tmp dir so tests never pollute the developer's real home.
import tempfile as _tempfile
os.environ.setdefault("NOUS_IMAGE_OUTPUTS", _tempfile.mkdtemp(prefix="nous-test-img-"))

# 测试库 = **每次运行一个新建的临时 PostgreSQL 库**,无条件改写 DATABASE_URL。
#
# 为什么是 PG 而不是 sqlite(2026-09-02 改):全局只用一种数据库。生产是 PG,测试
# 也跑 PG —— 测试环境与生产同构,"sqlite 过得去、PG 过不去"那类 bug 再也藏不住。
# 换掉之前踩过的:CI 慢 runner 上 sqlite 单写者锁超时,16 个写 DB 的用例成批红
# (#688),那是纯粹为迁就 sqlite 付的成本。
#
# 为什么是无条件(2026-08-29 起):旧逻辑只在 CI 的 `:memory:` 情况下换库,本地跑
# pytest 就直接读写 .env 里的**生产库**(nous_center)。实测一次全量跑在
# comfy_templates / service_instances 各留下 16 行测试垃圾(bridge-db-test /
# e2e-* / tpl-* / admin-e2e-*),而且 test_create_template_creates_service 硬编码
# 的名字 `minimax-h3-r2v` 撞上生产同名行 → 永久 409。这条不变式必须保住。
#
# 隔离粒度 = 每次 pytest 进程一个库(名字带随机后缀),退出时 DROP。并行跑互不干扰。
# 需要 nous 角色有 CREATEDB 权限(`ALTER ROLE nous CREATEDB`)。
#
# 逃生口:真要对着真库调试,显式 NOUS_TEST_USE_REAL_DB=1(护栏见
# tests/test_db_isolation.py,该变量置 1 时自动跳过)。
import asyncio
import atexit as _atexit
import secrets as _secrets
import urllib.parse as _urlparse

_use_real_db = os.environ.get("NOUS_TEST_USE_REAL_DB") == "1"
_test_db_name: str | None = None
_admin_url: str | None = None


def _pg_admin(sql: str) -> None:
    """用 asyncpg 直连 postgres 库执行一句管理 SQL(建库/删库/踢连接)。

    CREATE/DROP DATABASE 不能在事务里跑,asyncpg 的 execute 默认就是 autocommit,
    正好合用。走裸驱动而不是 SQLAlchemy —— 建库这步早于任何 engine,不想把
    连接池/方言那套牵进来。
    """
    import asyncio as _a  # noqa: PLC0415

    import asyncpg as _apg  # noqa: PLC0415

    dsn = (_admin_url or "").replace("postgresql+asyncpg://", "postgresql://")

    async def _run():
        conn = await _apg.connect(dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    _a.run(_run())


def _swap_in_temp_pg_database() -> None:
    """按 .env 的连接信息建一个 nous_test_<随机> 库,把 DATABASE_URL 指过去。"""
    global _test_db_name, _admin_url
    # .env 由 pydantic-settings 在实例化时读,**不进 os.environ** —— 所以这里要
    # 走 Settings 拿基准 URL,不能只看环境变量(环境变量优先,便于 CI 覆盖)。
    base = os.environ.get("DATABASE_URL") or ""
    if not base:
        from src.config import get_settings  # noqa: PLC0415
        base = get_settings().DATABASE_URL
        get_settings.cache_clear()   # 下面要改 DATABASE_URL,别让缓存里留旧值
    if not base.startswith("postgresql"):
        raise RuntimeError(
            f"测试要求 PostgreSQL,但解析到的 DATABASE_URL 是 {base[:40]!r} —— "
            "检查 backend/.env",
        )
    parsed = _urlparse.urlparse(base)
    # xdist 下每个 worker 是独立进程,各自走到这里建自己的库;名字带上 worker id
    # (gw0/gw1…,非 xdist 时为 main)方便从 pg_stat_activity 反查是哪个 worker 的。
    _worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    _test_db_name = f"nous_test_{_worker}_{_secrets.token_hex(4)}"
    # 建/删库连到已存在的 postgres 库。只装了 asyncpg(生产驱动),没有 psycopg2,
    # 所以这里直接用 asyncpg 裸连 + asyncio.run —— 此刻还没有事件循环在跑,安全。
    _admin_url = _urlparse.urlunparse(parsed._replace(path="/postgres"))
    _pg_admin(f'CREATE DATABASE "{_test_db_name}"')
    os.environ["DATABASE_URL"] = _urlparse.urlunparse(
        parsed._replace(path=f"/{_test_db_name}"))


def _drop_temp_pg_database() -> None:
    """进程退出时删掉临时库。失败只告警 —— 不能让清理把测试结果搞成失败。"""
    if not _test_db_name or not _admin_url:
        return
    try:
        # 先踢掉残留连接,否则 DROP 会因 "database is being accessed" 失败。
        _pg_admin(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{_test_db_name}' AND pid <> pg_backend_pid()")
        _pg_admin(f'DROP DATABASE IF EXISTS "{_test_db_name}"')
    except Exception as e:  # noqa: BLE001 — 清理失败不该影响测试结论
        print(f"[conftest] 清理临时测试库 {_test_db_name} 失败: {e}")


if not _use_real_db:
    _swap_in_temp_pg_database()
    _atexit.register(_drop_temp_pg_database)

import sys
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

# Lane J (spec §5.6): register shared integration/chaos fixtures so any
# test under tests/ can request `hardware_2gpu` / `hardware_3gpu` /
# `fake_runner` / `fake_vllm` without local imports.
pytest_plugins = [
    "tests.fixtures.hardware_topo",
    "tests.fixtures.fake_runner",
    "tests.fixtures.fake_vllm",
]

# Stub out heavy GPU dependencies so tests run without torch/torchaudio/etc.
for mod_name in [
    "torch", "torch.nn", "torch.nn.functional", "torch.cuda",
    "torchaudio", "torchaudio.transforms",
    "modelscope", "cosyvoice",
]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

from src.models.database import Base, get_async_session
from src.api.main import create_app
import src.models.voice_preset  # noqa: F401 — ensure models registered with Base
import src.models.tts_usage  # noqa: F401 — register model
import src.models.service_instance  # noqa: F401 — register model
import src.models.instance_api_key  # noqa: F401 — register model
import src.models.model_metadata  # noqa: F401 — register model
import src.models.workflow  # noqa: F401 — register model
import src.models.execution_task  # noqa: F401 — register model
import src.models.context_cache  # noqa: F401 — register model
import src.models.response_session  # noqa: F401 — register model
import src.models.model_runtime_override  # noqa: F401 — register model(数据加载统一)
import src.models.memory  # noqa: F401 — register model
import src.models.api_gateway  # noqa: F401 — register model
import src.models.status_sample  # noqa: F401 — register model(status 页采样)
# 下面 6 个以前没显式 import:5 个靠别的模块间接带进 Base.metadata,`task` 则**完全
# 没进** —— create_all 时表不存在,某个测试文件稍后 import 它,metadata 才多出
# `tasks`,此后每个 teardown 的 TRUNCATE 都撞 UndefinedTable(2026-09-02,1954 个 error)。
# 模型必须全部在这里 import,建表才建得齐;别依赖间接 import 的运气。
import src.models.admin_credentials  # noqa: E402,F401
import src.models.comfy_template  # noqa: E402,F401
import src.models.file  # noqa: E402,F401
import src.models.llm_usage  # noqa: E402,F401
import src.models.log_entry  # noqa: E402,F401
import src.models.task  # noqa: E402,F401

# Create the schema once on the isolated temp-file test DB (swapped in above).
# All models are imported by now, so Base.metadata is complete. Runs at import
# time — no event loop running yet — so asyncio.run is safe. Skipped when the
# NOUS_TEST_USE_REAL_DB escape hatch is on (that DB already has its schema).
if not _use_real_db:
    import asyncio as _asyncio

    from src.models.database import create_engine as _create_engine

    async def _init_ci_test_schema():
        _engine = _create_engine()
        async with _engine.begin() as _conn:
            await _conn.run_sync(Base.metadata.create_all)
        await _engine.dispose()

    _asyncio.run(_init_ci_test_schema())


@pytest.fixture(autouse=True)
async def _reset_memoized_session_factory():
    """round4 #1:database.get_session_factory() 进程级 memoize 共享工厂(修生产 engine
    泄漏)。测试间必须重置,否则一个测试首次调用绑定的工厂会泄漏到后续测试。

    光置 None 不够 —— 旧 engine 的连接池还挂着连接。以前各测试各用一个 schema,
    泄漏的连接指向别的 schema、互不相干;现在所有测试共用一套表,泄漏连接若带着
    未结束事务会卡住 TRUNCATE。所以这里**真 dispose**。"""
    import src.models.database as _db

    async def _dispose():
        f = _db._session_factory
        _db._session_factory = None
        if f is not None:
            bind = f.kw.get("bind")
            if bind is not None:
                try:
                    await bind.dispose()
                except Exception:  # noqa: BLE001 — 清理失败不该判测试失败
                    pass

    await _dispose()
    yield
    await _dispose()


@pytest.fixture(autouse=True)
def _reset_runtime_override_cache():
    """runtime_override_store 进程级 _CACHE(数据加载统一 2026-06-16):API 测试调
    set_override 会改全局缓存,测试间清掉防跨测试污染。"""
    from src.services import runtime_override_store
    runtime_override_store.reset_cache()
    yield
    runtime_override_store.reset_cache()


_COMFY_SIDECAR_OK = {
    "state": "ok", "online": True, "unit_active": True, "identity": "systemd",
    "listens": ["127.0.0.1"], "missing_listens": [], "problems": [], "checked_at": 0.0,
}


@pytest.fixture(autouse=True)
def _stub_comfy_sidecar_status(request, monkeypatch):
    """/health 与状态页都会做 ComfyUI sidecar 体检(systemctl / ss / 打 /queue)。
    测试里绝不依赖本机真有 ComfyUI 与 systemd 单元 —— 否则 CI(没有 sidecar)下
    /health 恒 degraded,一堆断言 status 的用例随环境红绿。默认打桩成 ok;
    要测真实实现的模块设 `REAL_COMFY_SIDECAR_STATUS = True`,改用自己的桩。"""
    from src.services.comfy import sidecar_status as mod
    mod.reset_cache()
    if not getattr(request.module, "REAL_COMFY_SIDECAR_STATUS", False):
        async def _ok():
            return dict(_COMFY_SIDECAR_OK)
        monkeypatch.setattr(mod, "sidecar_status", _ok)
    yield
    mod.reset_cache()


def _mock_model_manager():
    """Create a mock ModelManager for tests."""
    mgr = MagicMock()
    mgr.load_model = MagicMock(side_effect=lambda *a, **kw: _async_noop())
    mgr.unload_model = MagicMock(side_effect=lambda *a, **kw: _async_noop())
    mgr.add_reference = MagicMock()
    mgr.remove_reference = MagicMock()
    mgr.get_model_dependencies = MagicMock(return_value=[])
    mgr.loaded_model_ids = []
    mgr.get_status = MagicMock(return_value={"loaded": [], "references": {}, "last_used": {}})
    mgr.check_idle_models = MagicMock(side_effect=lambda: _async_noop())
    # 2026-09-05:这两个默认必须显式给,否则 MagicMock 自动造的子 mock 是**真值** ——
    # `is_loaded(x)` 恒真会让 /api/v1/engines 把所有引擎报成 loaded,`get_references(x)`
    # 也返回一个非空 mock。默认「没加载、没引用」才是干净起点。
    mgr.is_loaded = MagicMock(return_value=False)
    mgr.get_references = MagicMock(return_value=set())
    mgr.is_in_use = MagicMock(return_value=False)
    return mgr


async def _async_noop():
    pass


@pytest.fixture
def app():
    _app = create_app()
    _app.state.model_manager = _mock_model_manager()
    return _app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _test_engine():
    """给 fixture 用的、指向临时测试库的独立 engine。

    历史:换 PG 之初照搬了 sqlite「一测试一文件」的隔离思路 —— 每个 fixture 建一个
    独立 schema、跑完 DROP。实测 PG 建/删一套表要 57ms(sqlite 7ms),465 个用例
    累计多花 ~230s,是「换 PG 后测试慢一倍」的真正来源(单条查询 PG 反而更快:
    0.095ms vs 0.114ms)。

    现在:表在进程启动时建**一次**(见顶部 _init_ci_test_schema),所有 fixture 共用
    public schema;测试之间用 TRUNCATE 清数据(10ms,见 _truncate_after_each_test),
    表结构不动。
    """
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    # NullPool:每次连接现开现关。asyncpg 连接绑定创建它的 event loop,pytest-asyncio
    # 默认每个测试一个 loop —— 复用池里的旧连接会抛 "attached to a different loop"。
    return create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)


@pytest.fixture(autouse=True)
async def _drain_leftover_tasks(_truncate_after_each_test):
    """测试结束时回收它遗留的 asyncio 任务。

    异步 prediction 走 `asyncio.create_task(exec_coro)`(predictions.py:244),
    测试拿到 202 就返回了,那个任务还在跑。以前各测试各用一个 schema,泄漏任务写到
    哪都没人看;现在所有测试共用一套表,泄漏任务会两头咬人:
      * 还持着事务 → TRUNCATE 等锁 5s 超时(LockNotAvailableError)
      * 跑过 monkeypatch 拆除 → 桥去连真 sidecar(httpx ConnectError)
    同一根因、两种症状,而且看时序 —— 三次跑出三个结果。

    依赖 `_truncate_after_each_test` 是为了**排序**:pytest 先 setup 依赖再 setup
    本 fixture,teardown 反过来 → 本 fixture 先跑(cancel 任务),TRUNCATE 后跑。
    """
    yield
    cur = asyncio.current_task()
    leftover = [t for t in asyncio.all_tasks() if t is not cur and not t.done()]
    for t in leftover:
        t.cancel()
    if leftover:
        # return_exceptions:被 cancel 的任务抛 CancelledError 是预期,别让它冒泡
        await asyncio.wait(leftover, timeout=5.0)


@pytest.fixture
async def pg_engine():
    """给自建 engine 的测试用:指向临时测试库、表已建好的 PG engine。

    以前这些测试各写 `create_async_engine("sqlite+aiosqlite:///...")` 自己造库。
    全局改用 PostgreSQL 后不能再那么写(JSONB 等类型 sqlite 编译不了),统一走这里。
    """
    engine = _test_engine()
    try:
        yield engine
    finally:
        await engine.dispose()


async def _truncate_all_tables() -> None:
    """清空全部表(保留结构)。lock_timeout 兜底:要是哪个测试泄漏了未提交事务,
    TRUNCATE 会等锁 —— 与其无限挂着,不如 5s 后报错把泄漏者揪出来。"""
    from sqlalchemy import text as _text  # noqa: PLC0415

    engine = _test_engine()
    try:
        try:
            async with engine.begin() as conn:
                # 只清**库里真实存在**的表。Base.metadata 可能含晚 import 的模型
                # (某测试文件才 import src.models.xxx → metadata 多出一张 create_all 时
                # 还不存在的表)。硬列出来 TRUNCATE 会报 UndefinedTable,而且**之后每个
                # 测试的 teardown 都跟着倒**(2026-09-02 实测 1954 个 error 全是
                # `relation "tasks" does not exist`)。取交集就免疫这类漂移;真缺表会在
                # 用到它的那个测试里局部报错,更好定位。
                existing = {r[0] for r in await conn.execute(_text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
                names = ", ".join(f'"{t}"' for t in Base.metadata.tables if t in existing)
                if not names:
                    return
                await conn.execute(_text("SET LOCAL lock_timeout = '5s'"))
                await conn.execute(_text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        except Exception as e:  # noqa: BLE001 — 下面补上阻塞者再抛
            if "lock timeout" not in str(e):
                raise
            # 谁占着锁?把 pg_stat_activity 里同库的其它连接打出来 —— 泄漏事务的
            # 那条会显示 state='idle in transaction' 且 query 是它最后执行的 SQL,
            # 直接指向泄漏源。比猜"哪个测试没关 session"快得多。
            blockers = []
            async with engine.connect() as conn:
                rows = await conn.execute(_text(
                    "SELECT pid, state, wait_event_type, left(query, 120) AS q "
                    "FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"))
                blockers = [dict(r._mapping) for r in rows]
            raise RuntimeError(
                "TRUNCATE 等锁 5s 超时 —— 有测试泄漏了未结束的事务。"
                f"同库其它连接:{blockers}") from e
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate_after_each_test():
    """每个测试跑完清一次表 —— 这是测试间隔离的唯一机制(不再建/删 schema)。

    autouse 且不依赖别的 fixture → setup 最早、teardown **最晚**,保证跑到这里时
    测试自己的 engine 都已 dispose、事务都已结束,TRUNCATE 拿得到锁。
    """
    if _use_real_db:
        yield
        return
    yield
    await _truncate_all_tables()


@pytest.fixture
async def db_client():
    """Client with a real (PostgreSQL) test database for voice preset tests."""
    engine = _test_engine()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with session_factory() as session:
            yield session

    test_app = create_app()
    test_app.state.model_manager = _mock_model_manager()
    test_app.dependency_overrides[get_async_session] = override_session

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await engine.dispose()


@pytest.fixture
async def db_session():
    """Raw async session with all tables created (no app)."""
    engine = _test_engine()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        # 显式在**当前**事件循环里关掉 session,再拆 schema。交给 `async with` 的
        # 隐式关闭会在 teardown 的另一个 loop 里跑,asyncpg 抛
        # "attached to a different loop"。
        await session.close()
        await engine.dispose()

# Auto-use fixture: isolate tests from real logs.db
from .conftest_logs import _silence_db_log_handler  # noqa: F401


@pytest.fixture
async def sample_instance(db_session):
    from src.models.service_instance import ServiceInstance
    inst = ServiceInstance(
        source_type="model",
        source_name="qwen3.5-35b-test",
        name="test instance",
        type="llm",
        status="active",
    )
    db_session.add(inst)
    await db_session.commit()
    await db_session.refresh(inst)
    return inst


@pytest.fixture
async def other_instance(db_session):
    from src.models.service_instance import ServiceInstance
    inst = ServiceInstance(
        source_type="model",
        source_name="other-model",
        name="other instance",
        type="llm",
        status="active",
    )
    db_session.add(inst)
    await db_session.commit()
    await db_session.refresh(inst)
    return inst


@pytest.fixture
def on_progress_capture(monkeypatch):
    """Capture all events emitted via workflow_executor._on_progress_ref.

    Used by Wave 1 event-type tests. Resets the module-level reference so the
    LLM node's streaming branch routes events through our capture callable.
    """
    class _Cap:
        def __init__(self):
            self.events: list[dict] = []

        async def __call__(self, ev):
            self.events.append(ev)

    cap = _Cap()
    from src.services import workflow_executor
    monkeypatch.setattr(workflow_executor, "_on_progress_ref", cap)
    return cap


def _install_fake_adapter(monkeypatch, *, stream_tokens=("hel", "lo"),
                          usage=None, nonstream_text="non-stream response",
                          nonstream_usage=None):
    """Install a fake v2 adapter on workflow_executor._model_manager.

    LLMNode.stream / invoke now resolve the adapter via
    `mm.get_loaded_adapter(model_key)` and call `adapter.infer_stream(req)` /
    `adapter.infer(req)`. This helper replaces the module-level model manager
    with one that returns a fake adapter wired with predictable behavior.
    """
    from unittest.mock import AsyncMock, MagicMock

    from src.services import workflow_executor
    from src.services.inference.base import (
        InferenceResult,
        StreamEvent,
        UsageMeter,
    )

    stream_usage = usage or {
        "prompt_tokens": 2,
        "completion_tokens": 2,
        "total_tokens": 4,
    }
    nonstream_usage = nonstream_usage or {
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "total_tokens": 8,
    }

    async def _infer_stream(req):
        for tok in stream_tokens:
            yield StreamEvent(type="delta", payload={"content": tok})
        yield StreamEvent(type="done", payload={"usage": stream_usage})

    raw_body = {
        "choices": [{"message": {"content": nonstream_text}}],
        "usage": nonstream_usage,
    }

    async def _infer(req):
        return InferenceResult(
            media_type="application/json",
            data=b"{}",
            metadata={"raw": raw_body},
            usage=UsageMeter(
                input_tokens=nonstream_usage.get("prompt_tokens"),
                output_tokens=nonstream_usage.get("completion_tokens"),
                latency_ms=1,
            ),
        )

    adapter = MagicMock()
    adapter.is_loaded = True
    adapter.infer = _infer
    adapter.infer_stream = _infer_stream
    adapter.max_model_len = 4096

    mgr = MagicMock()
    mgr.get_loaded_adapter = AsyncMock(return_value=adapter)
    mgr.get_adapter = MagicMock(return_value=adapter)
    monkeypatch.setattr(workflow_executor, "_model_manager", mgr)
    return adapter


@pytest.fixture
def mock_llm_stream(monkeypatch):
    """Install a fake v2 adapter that streams two delta events ('hel','lo')
    and a final done event with usage. LLMNode.stream iterates infer_stream
    and reads the final usage from the done event.
    """
    return _install_fake_adapter(monkeypatch)


@pytest.fixture
def mock_llm_stream_v2(monkeypatch):
    """Alias of mock_llm_stream for tests still binding the v2 fixture name."""
    return _install_fake_adapter(monkeypatch)


@pytest.fixture
def mock_llm_nonstream(monkeypatch):
    """Install a fake v2 adapter whose infer() returns a canned non-stream
    OpenAI-format response wrapped in the InferenceResult envelope.
    """
    return _install_fake_adapter(monkeypatch)


@pytest.fixture
async def sample_api_key(db_session, sample_instance):
    """Returns the plaintext key string. Inserts the bcrypt-hashed row."""
    import bcrypt
    import secrets as _secrets
    from src.models.instance_api_key import InstanceApiKey

    raw = f"sk-test-{_secrets.token_hex(8)}"
    k = InstanceApiKey(
        instance_id=sample_instance.id,
        label="test",
        key_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
        key_prefix=raw[:10],
        is_active=True,
    )
    db_session.add(k)
    await db_session.commit()
    return raw


# ---------- Fixtures for /v1/responses agent binding tests ---------- #


@pytest.fixture
def fixtures_home(monkeypatch):
    """Point NOUS_CENTER_HOME at tests/fixtures for agent/skill lookups."""
    from pathlib import Path
    from src.config import get_settings
    from src.services.prompt_composer import _persona as _persona_mod

    fixtures = Path(__file__).parent / "fixtures"
    monkeypatch.setenv("NOUS_CENTER_HOME", str(fixtures))
    # Settings is cached; must clear so the new env var is picked up.
    get_settings.cache_clear()
    # persona _load_cached is keyed on (agent_id, mtime); clear to be safe in
    # case a prior test loaded a different fixtures_home.
    _persona_mod._load_cached.cache_clear()
    return fixtures


@pytest.fixture
def mock_vllm(monkeypatch):
    """Intercept outgoing httpx requests to vLLM, record last body.

    Patches ``httpx.AsyncClient.post`` class-wide but only rewrites responses
    whose URL does NOT target the ASGI test transport (base ``http://test``).
    Internal ASGI calls ``api_client.post("/v1/responses", ...)`` still go to
    the real httpx POST path; only outbound vLLM calls get mocked.

    Also stubs out record_llm_usage (which creates a separate engine against
    the real DATABASE_URL) so tests don't require Postgres.
    """
    import httpx

    real_post = httpx.AsyncClient.post

    class _Recorder:
        def __init__(self):
            self.last_request_body: dict | None = None
            self.last_usage: dict | None = None

    recorder = _Recorder()

    async def _patched_post(self, url, *args, **kwargs):
        # Distinguish outbound (upstream vLLM) vs inbound (ASGI test transport)
        # by looking at the full request URL. Inbound api_client.post uses the
        # AsyncClient's base_url "http://test", so the resolved URL has host
        # "test" — never "test-vllm.invalid". httpx merges client.base_url with
        # relative paths, and the "url" arg to AsyncClient.post can be a bare
        # path — we must inspect the merged absolute form, not the raw arg.
        from httpx import URL as _URL
        base = getattr(self, "base_url", None)
        raw = _URL(url) if not isinstance(url, _URL) else url
        full = base.join(raw) if base else raw
        host = full.host or ""
        if host == "test-vllm.invalid" or (not host and "test-vllm.invalid" in str(url)):
            body = kwargs.get("json")
            recorder.last_request_body = body
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-mock",
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                        },
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    },
                },
                request=httpx.Request("POST", url),
            )
        return await real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", _patched_post)

    # Stub record_llm_usage — it opens its own engine against DATABASE_URL.
    async def _noop_record(**kwargs):
        recorder.last_usage = kwargs
    import src.services.usage_service as _usage
    monkeypatch.setattr(_usage, "record_llm_usage", _noop_record)

    return recorder


@pytest.fixture
async def api_client():
    """Async client with a SQLite-backed app, a loaded-LLM adapter mock,
    and a LLM-type service instance + API key preseeded.

    Exposes:
        api_client.app.state.async_session_factory  — for tests to query DB
        api_client.headers default Authorization Bearer <key>
    """
    import bcrypt
    import secrets as _secrets
    from unittest.mock import MagicMock

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.api.main import create_app
    from src.models.api_gateway import ApiKeyGrant
    from src.models.database import get_async_session
    from src.models.instance_api_key import InstanceApiKey
    from src.models.service_instance import ServiceInstance

    engine = _test_engine()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Seed a model-type service + an active M:N API key + a grant.
    # legacy rip:默认 key 由 legacy 1:1(instance_id 绑定、忽略 req.model)改 M:N(instance_id=None + grant)。
    # service name 必须 == 测试请求的 model("qwen3.5") —— M:N 解析按 ServiceInstance.name 匹配 request.model。
    raw_key = f"sk-test-{_secrets.token_hex(8)}"
    async with session_factory() as s:
        inst = ServiceInstance(
            source_type="model",
            source_name="qwen3.5",
            name="qwen3.5",
            type="llm",
            status="active",
        )
        s.add(inst)
        await s.commit()
        await s.refresh(inst)
        key = InstanceApiKey(
            instance_id=None,
            label="test",
            key_hash=bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt()).decode(),
            key_prefix=raw_key[:10],
            is_active=True,
        )
        s.add(key)
        await s.commit()
        await s.refresh(key)
        s.add(ApiKeyGrant(api_key_id=key.id, service_id=inst.id, status="active"))
        await s.commit()

    async def override_session():
        async with session_factory() as session:
            yield session

    # Force services that create their own engine (usage_service,
    # responses.py's `_csf = create_session_factory`) to use the SQLite engine.
    import src.models.database as _db_mod
    orig_create_session_factory = _db_mod.create_session_factory

    def _patched_csf(engine_arg=None):
        return session_factory

    _db_mod.create_session_factory = _patched_csf

    test_app = create_app()

    # Mock model_manager to return a loaded adapter for engine_name 'qwen3.5'.
    mgr = MagicMock()
    adapter = MagicMock()
    adapter.is_loaded = True
    adapter.base_url = "http://test-vllm.invalid"
    adapter.max_model_len = 4096
    mgr.get_adapter = MagicMock(return_value=adapter)
    test_app.state.model_manager = mgr
    # Expose session factory for tests.
    test_app.state.async_session_factory = session_factory

    test_app.dependency_overrides[get_async_session] = override_session

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # Stash app + raw_key so tests/fixtures can find them.
        c.app = test_app  # type: ignore[attr-defined]
        c.raw_key = raw_key  # type: ignore[attr-defined]
        yield c

    _db_mod.create_session_factory = orig_create_session_factory
    await engine.dispose()


@pytest.fixture
def bearer_headers(api_client):
    return {"Authorization": f"Bearer {api_client.raw_key}"}

