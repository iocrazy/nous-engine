
import pytest

from src.config import Settings, load_model_configs, _resolve_path


def test_settings_defaults():
    # Use _env_file=None to prevent .env from overriding defaults
    settings = Settings(
        _env_file=None,
        REDIS_URL="redis://localhost:6379/0",
        DATABASE_URL="postgresql+asyncpg://x:x@localhost/db",
    )
    assert settings.REDIS_URL == "redis://localhost:6379/0"
    assert settings.VLLM_BASE_URL == "http://localhost:8100"


def test_paths_derive_from_roots_excluding_lora():
    """从 test_paths_derive_from_roots 挪来(2026-09-27,该测试整体删除因为它混了一条
    LORA_PATHS 断言,而约束只允许整删用例、不许改断言):其余 5 条非 LORA_PATHS 断言
    逐字保留。原 docstring:路径收口(spec 2026-06-19):子根从 MODELS_ROOT/REPOS_ROOT
    + model_roots.yaml 派生。
    """
    s = Settings(
        _env_file=None,
        REDIS_URL="r",
        DATABASE_URL="d",
        MODELS_ROOT="/data/models",
        REPOS_ROOT="/data/repos",
    )
    assert s.LOCAL_MODELS_PATH == "/data/models/nous"
    assert s.NAS_MODELS_PATH == "/data/models/nous"  # NAS 并进本地根
    assert s.NAS_OUTPUTS_PATH == "/data/models/nous/outputs"
    assert s.COSYVOICE_REPO_PATH == "/data/repos/CosyVoice"
    assert s.INDEXTTS_REPO_PATH == "/data/repos/index-tts"


def test_local_models_path_derives_regardless_of_other_overrides():
    """从 test_explicit_path_override_wins 挪来(2026-09-26,该测试整体删除因为它专测
    LORA_PATHS 的显式覆盖):「其余仍派生」这条非 LORA_PATHS 断言逐字保留。"""
    s = Settings(
        _env_file=None,
        REDIS_URL="r",
        DATABASE_URL="d",
        MODELS_ROOT="/data/models",
    )
    assert s.LOCAL_MODELS_PATH == "/data/models/nous"  # 其余仍派生


def test_load_model_configs():
    configs = load_model_configs("configs/models.yaml")
    assert "cosyvoice2" in configs
    assert configs["cosyvoice2"]["type"] == "tts"
    assert "qwen3_tts_base" in configs


def test_resolve_path_is_relative_to_backend():
    """Paths must resolve relative to backend/ dir, not cwd."""
    resolved = _resolve_path("configs/models.yaml")
    assert "backend" in str(resolved), f"Expected path relative to backend/, got {resolved}"


def test_load_model_configs_from_any_cwd(tmp_path, monkeypatch):
    """load_model_configs works even when cwd is not backend/."""
    monkeypatch.chdir(tmp_path)
    configs = load_model_configs()
    assert isinstance(configs, dict)


def test_load_settings_yaml_non_dict_returns_empty(tmp_path, monkeypatch):
    """round4 #5:settings.yaml 手改成 YAML 列表/标量 → 降级空 dict,不让 Settings(**) 崩。"""
    import src.config as cfg
    p = tmp_path / "settings.yaml"
    p.write_text("- a\n- b\n")  # YAML 列表
    monkeypatch.setattr(cfg, "SETTINGS_YAML_PATH", p)
    assert cfg._load_settings_yaml() == {}
    # 标量
    p.write_text("just a string\n")
    assert cfg._load_settings_yaml() == {}
    # 正常 dict 仍工作
    p.write_text("FOO: bar\n")
    assert cfg._load_settings_yaml() == {"FOO": "bar"}


async def test_runtime_override_db_roundtrip(pg_engine):
    """数据加载统一(2026-06-16):set_override 写 DB(typed 列)→ hydrate → get_overrides
    读回同形状 dict。load_runtime_overrides 走该缓存。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.services import runtime_override_store as store

    engine = pg_engine   # 全局只用 PostgreSQL;表由 pg_engine fixture 建好
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        store.reset_cache()
        async with sf() as s:
            await store.set_override(s, "m1", "resident", True)
            await store.set_override(s, "m2", "gpu", 1)
            await store.set_override(s, "m1", "gpu", 0)  # 同 model 不同列,合并不覆盖
            await store.set_override(s, "m3", "vram_budget", {"mode": "absolute", "value": 22.0})
        # write-through 缓存
        # gpus: [] = 钉单卡时写的「显式清空组」哨兵(区分"没覆盖过"→ 回退 YAML)。
        assert store.get_overrides()["m1"] == {"resident": True, "gpu": 0, "gpus": []}
        assert store.get_overrides()["m2"] == {"gpu": 1, "gpus": []}
        assert store.get_overrides()["m3"] == {"vram_budget": {"mode": "absolute", "value": 22.0}}
        # 重新 hydrate(模拟重启)→ 从 DB 还原一致
        store.reset_cache()
        assert store.get_overrides() == {}
        await store.hydrate(sf)
        assert store.get_overrides()["m1"] == {"resident": True, "gpu": 0, "gpus": []}
        assert store.get_overrides()["m3"]["vram_budget"]["value"] == 22.0
    finally:
        store.reset_cache()
        pass  # engine 由 pg_engine fixture 释放


async def test_runtime_override_rejects_non_overridable_key():
    """只白名单 resident/gpu/vram_budget,别让随意键污染覆盖(键检查在碰 DB 前)。"""
    from src.services import runtime_override_store as store
    with pytest.raises(ValueError):
        await store.set_override(None, "m1", "type", "evil")  # 检查先于 session 使用


async def test_runtime_override_migrate_corrupt_json_is_soft(tmp_path, pg_engine):
    """一次性迁移读坏 JSON 不该拖垮启动 → 返回 0,不抛。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.services import runtime_override_store as store

    engine = pg_engine   # 全局只用 PostgreSQL;表由 pg_engine fixture 建好
    sf = async_sessionmaker(engine, expire_on_commit=False)
    bad = tmp_path / "runtime_overrides.json"
    bad.write_text("{not json")
    try:
        assert await store.migrate_json_if_empty(sf, str(bad)) == 0
    finally:
        pass  # engine 由 pg_engine fixture 释放


def test_runtime_override_overlays_model_configs(monkeypatch):
    """overlay 优先叠加到 load_model_configs 的结果(只对已存在的 model 生效)。"""
    import src.config as cfg
    monkeypatch.setattr(
        cfg, "load_runtime_overrides",
        lambda: {"m1": {"resident": True}, "ghost": {"resident": True}},
    )
    cfgs = {"m1": {"resident": False}, "m2": {"resident": False}}
    cfg._apply_runtime_overrides(cfgs)
    assert cfgs["m1"]["resident"] is True   # overlay 生效
    assert cfgs["m2"]["resident"] is False  # 未覆盖的不动
    assert "ghost" not in cfgs              # overlay 里有但 cfgs 没有 → 不凭空建条目
