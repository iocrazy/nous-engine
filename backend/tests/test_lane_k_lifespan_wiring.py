"""Lane K: lifespan 真把 RunnerSupervisor / LLMRunner / RunnerClient 接进 app.state。

V1.5 的 12 个 lane 全部交付了零件（RunnerSupervisor / LLMRunner / RunnerClient /
CancelFlag / image adapter callback / GroupScheduler），但 lifespan 里没人
instantiate 它们 —— smoke 实测 `app.state.runner_supervisors` 全仓零 write，
`/api/v1/monitor/runners` 返回 `{"runners": []}`，image workflow dispatch 撞
`runner_client is None` 报错。本 lane 把这层 wiring 补上。

测试不能真 spawn runner 子进程（multiprocessing + 真模型加载），
所以 monkeypatch `RunnerSupervisor._spawn` 为 no-op + 注入 fake gpu probe，
让 lifespan 把对象塞进 app.state 但不起任何真实进程/网络。

DB 注意:lifespan 跑全套 DB 初始化(create_all + voice-preset migration +
metadata sync)。CI 用 `:memory:` SQLite,每次 create_engine() 新进程私有 →
表会丢。(历史)本测试曾把 DATABASE_URL 指向 tmp_path 下的文件 sqlite;现在全局 PG,
保证 lifespan 内多次 create_engine() 看到同一份表结构。
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def temp_pg_db(monkeypatch, tmp_path):
    """让 lifespan 跑在 conftest 建的临时 PG 库上(前身叫 file_sqlite_db)。

    以前这里把 DATABASE_URL 换成 tmp_path 下的文件 sqlite —— 因为 `:memory:`
    sqlite 是 per-connection 的,lifespan 多次 create_engine() 会各拿一个空库。
    全局改用 PostgreSQL 后 DATABASE_URL 本来就是一个稳定的共享临时库,不需要再换;
    只保留"清缓存"这部分,保证 lifespan 读到的是当前 URL、不复用陈旧 engine。
    """
    db_url = os.environ["DATABASE_URL"]
    from src.config import get_settings
    get_settings.cache_clear()
    import src.models.database as _db_mod
    monkeypatch.setattr(_db_mod, "_session_factory", None, raising=False)
    yield db_url
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_lifespan_populates_runner_supervisors_clients_and_llm_runner(
    monkeypatch, tmp_path, temp_pg_db
):
    """Lane K 主断言：开启 runner spawn gate 后，lifespan 应在 app.state 上塞好：
      * runner_supervisors —— image / tts group 各一个（不包含 llm group）
      * runner_clients     —— group_id → client（image / tts）
      * llm_runner         —— role:llm group 的 LLMRunner 对象（singular，per spec §4.1）
    """
    # 启用 Lane K spawn 路径（默认 conftest 关，避免拖慢/破坏其它测试）。
    monkeypatch.setenv("NOUS_DISABLE_RUNNER_SPAWN", "0")
    # 背景任务整体禁用维持 conftest 默认（=1），Lane K 的 runner spawn 不应该被
    # NOUS_DISABLE_BG_TASKS 一并 kill —— runner spawn 是 V1.5 生产数据面，
    # 不能跟 idle_checker / log_cleanup 等无关循环共用一个 kill switch。

    # 写一份 3-group 测试 hardware.yaml（llm + image + tts），并把 loader 切到它。
    hw_yaml = tmp_path / "hardware.yaml"
    hw_yaml.write_text(
        "groups:\n"
        "  - id: llm\n    gpus: [1]\n    nvlink: false\n    role: llm\n    vram_gb: 96\n"
        "  - id: image\n    gpus: [0]\n    nvlink: false\n    role: image\n    vram_gb: 24\n"
        "  - id: tts\n    gpus: [2]\n    nvlink: false\n    role: tts\n    vram_gb: 24\n"
    )

    # GPUAllocator 默认调 load_hardware_config()，没参数 → 走 lru_cache。
    # 清掉 cache 再 monkeypatch 函数到我们的临时 yaml。
    from src import config as _config
    if hasattr(_config.load_hardware_config, "cache_clear"):
        _config.load_hardware_config.cache_clear()
    import yaml
    monkeypatch.setattr(
        _config, "load_hardware_config",
        lambda *a, **kw: yaml.safe_load(hw_yaml.read_text())
    )

    # Stub RunnerSupervisor._spawn —— 不起真进程；标记 spawn 被调用过。
    spawn_calls: list[str] = []
    from src.runner import supervisor as _sup_mod

    async def _fake_spawn(self):
        spawn_calls.append(self.group_id)
        # 让 is_running 返回 True：假 process + 假 client。
        class _FakeProc:
            def is_alive(self):
                return True
            pid = 9999
        class _FakeClient:
            is_connected = True
            async def close(self):
                pass
            async def ping(self):
                return None
        self._process = _FakeProc()
        self.client = _FakeClient()
        import time as _t
        self._last_spawn_at = _t.monotonic()

    monkeypatch.setattr(_sup_mod.RunnerSupervisor, "_spawn", _fake_spawn)

    # Watchdog 是 ping_interval 跑一次 ping 的 background task；测试里我们不需要
    # 它真跑 —— stub 成立即返回的 coroutine。
    async def _noop_watchdog(self):
        return None
    monkeypatch.setattr(_sup_mod.RunnerSupervisor, "_watchdog", _noop_watchdog)

    # Build app + drive lifespan manually. ASGITransport does NOT trigger lifespan,
    # so we open the lifespan context directly (Starlette's lifespan is an
    # @asynccontextmanager).
    from src.api.main import create_app, lifespan as _lifespan

    app = create_app()
    async with _lifespan(app):
        # ---- 主断言 ----
        supervisors = getattr(app.state, "runner_supervisors", None)
        assert supervisors is not None, \
            "Lane K should populate app.state.runner_supervisors during lifespan"
        # 2 个非 llm group:image + tts。
        sup_groups = sorted(s.group_id for s in supervisors)
        assert sup_groups == ["image", "tts"], \
            f"expected supervisors for image+tts only, got {sup_groups}"

        clients = getattr(app.state, "runner_clients", None)
        assert clients is not None, \
            "Lane K should populate app.state.runner_clients"
        assert set(clients.keys()) == {"image", "tts"}, \
            f"expected runner_clients keyed image+tts, got {set(clients.keys())}"

        # LLMRunner —— 单个对象（spec §4.1:每个 role:llm group 一个）。
        llm = getattr(app.state, "llm_runner", None)
        assert llm is not None, \
            "Lane K should populate app.state.llm_runner for the role:llm group"
        # 必带 llm_gpus（从 hardware.yaml 取 [1]）。
        assert getattr(llm, "llm_gpus", None) == [1]

        # 确认 spawn 真的被调过（=不是空 list 占位）。
        assert sorted(spawn_calls) == ["image", "tts"]


@pytest.mark.asyncio
async def test_lifespan_skips_runner_spawn_by_default(temp_pg_db):
    """默认 conftest 的 NOUS_DISABLE_BG_TASKS=1 + 无 NOUS_DISABLE_RUNNER_SPAWN
    覆盖时,Lane K 不应起任何 supervisor —— 保证现有 fast 测试套不变慢/不被破坏。

    断言反向:lifespan 跑完后 runner_supervisors 是空 list（或未设置/空,皆 OK）。
    """
    # 不动 env vars —— 默认 conftest.py 已经 setdefault NOUS_DISABLE_BG_TASKS=1，
    # 这条 path 应该跳过整个 runner spawn 块。
    from src.api.main import create_app, lifespan as _lifespan

    app = create_app()
    async with _lifespan(app):
        supervisors = getattr(app.state, "runner_supervisors", None)
        # 允许 None（未设置）或空 list —— 关键是不能有真 supervisor。
        assert not supervisors, \
            f"runner_supervisors should be empty by default, got {supervisors}"


@pytest.mark.asyncio
async def test_routes_read_runner_clients_by_group(monkeypatch, tmp_path):
    """Lane K 第二个集成点：/run 端点要按节点 role 选 RunnerClient,不能再读旧的
    `app.state.runner_client`（singular）。本测试断言代码层而不是发请求。

    具体:workflow_runner.run_workflow_task 应能从 app.state.runner_clients dict
    里挑出 image 节点对应的 client；instance_run / execute_workflow_direct 应能
    把这个 dict 传下去。
    """
    # 这里我们只检查 routes 模块的 lookup 行为 —— route 不再写死
    # `getattr(state, "runner_client", None)`,而是看 `runner_clients` dict。
    import importlib

    # 重新 import routes 以拿到最新源码。
    workflows_mod = importlib.reload(importlib.import_module("src.api.routes.workflows"))
    # 服务层 API spec PR-2:旧 instance_service.instance_run 已删,workflow 服务调用迁到
    # predictions.create_prediction —— 它同样要按 group 读 runner_clients dict。
    predictions_mod = importlib.reload(
        importlib.import_module("src.api.routes.predictions")
    )
    import inspect

    src1 = inspect.getsource(workflows_mod.execute_workflow_direct)
    # 提交核心 2026-09-26 搬到 src/api/prediction_submit.py(与 /v1/skill-runs/generate 共用);
    # create_prediction 经 submit_prediction 走同一条 runner_clients 路径。
    submit_mod = importlib.reload(importlib.import_module("src.api.prediction_submit"))
    assert "submit_prediction(" in inspect.getsource(predictions_mod.create_prediction)
    src2 = inspect.getsource(submit_mod.submit_prediction)

    # Lane K 之后,两条 workflow 执行路径都要看 runner_clients dict（按 group/role 路由）。
    # 旧实现读单个 `runner_client`(singular) 必须被替换 —— 否则永远拿不到注入的 per-group client。
    assert "runner_clients" in src1, \
        "execute_workflow_direct should read app.state.runner_clients (dict) post-Lane K"
    assert "runner_clients" in src2, \
        "prediction_submit.submit_prediction should read app.state.runner_clients (dict) post-Lane K"
