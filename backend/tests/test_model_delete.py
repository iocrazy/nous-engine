"""引擎库模型物理删除 —— spec docs/superpowers/specs/2026-07-28-model-physical-delete-design.md

全程 monkeypatch LOCAL_MODELS_PATH 到 tmp_path 造假模型树,**绝不碰真模型盘**。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _stub_roots(tmp_path, monkeypatch, lora_root=None):
    """把 model_deleter 的 settings 指到 tmp 树。返回 settings mock。"""
    from src.services import model_deleter as mod

    settings = MagicMock()
    settings.LOCAL_MODELS_PATH = str(tmp_path)
    settings.LORA_PATHS = str(lora_root) if lora_root else str(tmp_path / "media" / "loras")
    monkeypatch.setattr(mod, "get_settings", lambda: settings)
    return settings


def _make_model_dir(base, rel: str):
    d = base / rel
    d.mkdir(parents=True)
    (d / "config.json").write_text('{"model_type": "qwen3"}')
    (d / "model.safetensors").write_bytes(b"x" * 1024)
    return d


# ── resolve_target ────────────────────────────────────────────────────────


def test_resolve_registry_model_points_at_local_path_dir(tmp_path, monkeypatch):
    """kind=model 的条目 → LOCAL_MODELS_PATH/<local_path> 整目录。"""
    _make_model_dir(tmp_path, "llm/Qwen3.6-35B-A3B-FP8")
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import resolve_target

    t = resolve_target(
        "qwen3_6_35b_a3b_fp8",
        {"qwen3_6_35b_a3b_fp8": {"local_path": "llm/Qwen3.6-35B-A3B-FP8"}},
    )

    assert t.kind == "model"
    assert t.is_dir is True
    assert t.engine_key == "qwen3_6_35b_a3b_fp8"
    assert t.path == tmp_path / "llm/Qwen3.6-35B-A3B-FP8"


def test_resolve_seedvr2_points_at_single_dit_file(tmp_path, monkeypatch):
    """seedvr2:<filename> → media/SEEDVR2/<filename> 单文件。"""
    d = tmp_path / "media/SEEDVR2"
    d.mkdir(parents=True)
    (d / "seedvr2_ema_7b_fp8.safetensors").write_bytes(b"x" * 512)
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import resolve_target

    t = resolve_target("seedvr2:seedvr2_ema_7b_fp8.safetensors", {})

    assert t.kind == "upscale"
    assert t.is_dir is False
    assert t.engine_key is None
    assert t.path == d / "seedvr2_ema_7b_fp8.safetensors"


def test_resolve_component_uses_abs_path_from_name(tmp_path, monkeypatch):
    """component:<role>:<abs_path> → 该 abs_path 单文件;role=loras 归 kind=lora。"""
    d = tmp_path / "media/loras"
    d.mkdir(parents=True)
    f = d / "some-lora.safetensors"
    f.write_bytes(b"x" * 256)
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import resolve_target

    t = resolve_target(f"component:loras:{f}", {})

    assert t.kind == "lora"
    assert t.is_dir is False
    assert t.path == f


def test_resolve_unknown_engine_key_raises_404(tmp_path, monkeypatch):
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import DeleteError, resolve_target

    with pytest.raises(DeleteError) as exc:
        resolve_target("no_such_model", {})

    assert exc.value.status_code == 404


def test_resolve_rejects_dotdot_in_name_before_touching_disk(tmp_path, monkeypatch):
    """name 里带 .. 直接 400 —— 不进解析,不 stat 磁盘。"""
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import DeleteError, resolve_target

    with pytest.raises(DeleteError) as exc:
        resolve_target("seedvr2:../../../etc/passwd", {})

    assert exc.value.status_code == 400


# ── assert_safe_target ────────────────────────────────────────────────────


def test_safe_target_accepts_model_dir_under_models_root(tmp_path, monkeypatch):
    _make_model_dir(tmp_path, "llm/Qwen3.6-35B-A3B-FP8")
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import Target, assert_safe_target

    assert_safe_target(
        Target("k", "model", tmp_path / "llm/Qwen3.6-35B-A3B-FP8", True)
    )  # 不抛即通过


def test_safe_target_rejects_path_outside_all_roots(tmp_path, monkeypatch):
    """component 条目里塞绝对路径 → 落在模型根之外必须 400。"""
    outside = tmp_path.parent / "outside-victim.safetensors"
    outside.write_bytes(b"important")
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import DeleteError, Target, assert_safe_target

    with pytest.raises(DeleteError) as exc:
        assert_safe_target(Target("c", "component", outside, False))

    assert exc.value.status_code == 400
    assert outside.exists()  # 真实文件毫发无伤


def test_safe_target_rejects_models_root_itself(tmp_path, monkeypatch):
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import DeleteError, Target, assert_safe_target

    with pytest.raises(DeleteError) as exc:
        assert_safe_target(Target("x", "model", tmp_path, True))

    assert exc.value.status_code == 400


def test_safe_target_rejects_type_dir_at_depth_one(tmp_path, monkeypatch):
    """不许删 image/ llm/ speech/ 这类类型目录 —— 相对深度必须 >= 2。"""
    (tmp_path / "media").mkdir()
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import DeleteError, Target, assert_safe_target

    with pytest.raises(DeleteError) as exc:
        assert_safe_target(Target("x", "model", tmp_path / "media", True))

    assert exc.value.status_code == 400


def test_safe_target_downgrades_symlinked_dir_to_link_only(tmp_path, monkeypatch):
    """目标是软链 → is_dir 降为 False,后续只 unlink 链接,绝不顺着链接 rmtree。"""
    real = tmp_path.parent / "real-model-dir"
    real.mkdir(exist_ok=True)
    (real / "weights.safetensors").write_bytes(b"x" * 64)
    link = tmp_path / "llm" / "linked-model"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import Target, assert_safe_target

    t = Target("k", "model", link, True)
    assert_safe_target(t)

    assert t.is_dir is False


def test_safe_target_accepts_lora_under_separate_lora_root(tmp_path, monkeypatch):
    """LORA_PATHS 可以指向 LOCAL_MODELS_PATH 之外的根,该根一并进白名单。"""
    models = tmp_path / "nous"
    lora_root = tmp_path / "comfyui" / "models" / "loras"
    (lora_root / "flux").mkdir(parents=True)
    f = lora_root / "flux" / "style.safetensors"
    f.write_bytes(b"x" * 32)
    models.mkdir()
    _stub_roots(models, monkeypatch, lora_root=lora_root)

    from src.services.model_deleter import Target, assert_safe_target

    assert_safe_target(Target("c", "lora", f, False))  # 不抛即通过


# ── delete_disk ───────────────────────────────────────────────────────────


def test_delete_disk_removes_model_dir_and_reports_freed_bytes(tmp_path, monkeypatch):
    d = _make_model_dir(tmp_path, "llm/Qwen3.6-35B-A3B-FP8")
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import Target, delete_disk

    freed, errors = delete_disk(Target("k", "model", d, True))

    assert not d.exists()
    assert errors == []
    assert freed >= 1024


def test_delete_disk_removes_single_file(tmp_path, monkeypatch):
    d = tmp_path / "media/SEEDVR2"
    d.mkdir(parents=True)
    f = d / "dit.safetensors"
    f.write_bytes(b"x" * 700)
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import Target, delete_disk

    freed, errors = delete_disk(Target("s", "upscale", f, False))

    assert not f.exists()
    assert d.exists()  # 只删文件,不碰所在目录
    assert errors == []
    assert freed == 700


def test_delete_disk_on_symlink_leaves_real_target_intact(tmp_path, monkeypatch):
    """软链条目只删链接本身,链接指向的真实目录必须原封不动。"""
    real = tmp_path.parent / "symlink-victim-dir"
    real.mkdir(exist_ok=True)
    (real / "weights.safetensors").write_bytes(b"x" * 64)
    link = tmp_path / "llm" / "linked-model"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import Target, assert_safe_target, delete_disk

    t = Target("k", "model", link, True)
    assert_safe_target(t)  # 把 is_dir 降为 False
    _freed, errors = delete_disk(t)

    assert not link.exists() and not link.is_symlink()
    assert (real / "weights.safetensors").exists()
    assert errors == []


def test_delete_disk_reports_partial_failure_instead_of_raising(tmp_path, monkeypatch):
    """rmtree 删不动的项进 errors 而不是抛 —— 保证调用方仍能继续做注册表清理。"""
    d = _make_model_dir(tmp_path, "llm/Locked-Model")
    locked = d / "locked"
    locked.mkdir()
    (locked / "inner.bin").write_bytes(b"x" * 16)
    locked.chmod(0o500)  # r-x:目录内文件删不掉
    _stub_roots(tmp_path, monkeypatch)

    from src.services.model_deleter import Target, delete_disk

    try:
        _freed, errors = delete_disk(Target("k", "model", d, True))
        assert errors, "删不动的项必须如实报到 errors,不能静默"
    finally:
        if locked.exists():
            locked.chmod(0o700)


# ── scan_code_refs ────────────────────────────────────────────────────────


def test_scan_code_refs_finds_term_in_git_repo(tmp_path):
    """git 仓库走 `git grep`,命中带文件/行号/原文。"""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "routes.py").write_text(
        "CATEGORY = 'x'\nENGINE = 'qwen3_6_35b_a3b_fp8'\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    from src.services.model_deleter import scan_code_refs

    out = scan_code_refs(["qwen3_6_35b_a3b_fp8"], repo_root=tmp_path)

    assert out["scan_error"] is None
    hits = [r for r in out["refs"] if r["file"] == "src/routes.py"]
    assert hits and hits[0]["line"] == 2
    assert "qwen3_6_35b_a3b_fp8" in hits[0]["text"]


def test_scan_code_refs_falls_back_outside_git(tmp_path):
    """非 git 目录 → walk fallback 仍能找到引用。"""
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "a.yaml").write_text("model: my_retired_model\n")

    from src.services.model_deleter import scan_code_refs

    out = scan_code_refs(["my_retired_model"], repo_root=tmp_path)

    assert out["scan_error"] is None
    assert any(r["file"] == "conf/a.yaml" for r in out["refs"])


def test_scan_code_refs_fallback_skips_vendor_and_binary_dirs(tmp_path):
    """fallback 必须跳过 .venv/node_modules/dist —— 否则一个词能扫出上万条噪音。"""
    for junk in (".venv", "node_modules", "dist", "__pycache__"):
        (tmp_path / junk).mkdir()
        (tmp_path / junk / "x.py").write_text("my_retired_model\n")
    (tmp_path / "real.py").write_text("my_retired_model\n")

    from src.services.model_deleter import scan_code_refs

    out = scan_code_refs(["my_retired_model"], repo_root=tmp_path)

    assert [r["file"] for r in out["refs"]] == ["real.py"]


def test_scan_code_refs_drops_short_noisy_terms(tmp_path):
    """长度 < 4 的搜索词丢弃 —— 否则 'vae' 这种词满仓命中。"""
    (tmp_path / "a.py").write_text("vae = 1\n")

    from src.services.model_deleter import scan_code_refs

    out = scan_code_refs(["vae"], repo_root=tmp_path)

    assert out["refs"] == []


def test_scan_code_refs_truncates_at_limit(tmp_path):
    (tmp_path / "big.py").write_text("my_retired_model\n" * 50)

    from src.services.model_deleter import scan_code_refs

    out = scan_code_refs(["my_retired_model"], repo_root=tmp_path, limit=10)

    assert len(out["refs"]) == 10
    assert out["truncated"] is True


# ── 注册表清理:models.d/<key>.yaml ─────────────────────────────────────────


def test_delete_models_d_yaml_removes_the_file(tmp_path):
    md = tmp_path / "models.d"
    md.mkdir()
    f = md / "qwen3_6_35b_a3b_fp8.yaml"
    f.write_text("id: qwen3_6_35b_a3b_fp8\n")

    from src.services.model_deleter import delete_models_d_yaml

    assert delete_models_d_yaml("qwen3_6_35b_a3b_fp8", models_d=md) is True
    assert not f.exists()


def test_delete_models_d_yaml_absent_is_not_an_error(tmp_path):
    """自动发现的模型没有 yaml —— 返回 False,不抛。"""
    md = tmp_path / "models.d"
    md.mkdir()

    from src.services.model_deleter import delete_models_d_yaml

    assert delete_models_d_yaml("auto_detected_thing", models_d=md) is False


def test_delete_models_d_yaml_rejects_key_with_path_separator(tmp_path):
    """engine key 里带路径分隔符 → 拒绝,别让它逃出 models.d 目录。"""
    md = tmp_path / "models.d"
    md.mkdir()
    victim = tmp_path / "models.yaml"
    victim.write_text("models: []\n")

    from src.services.model_deleter import DeleteError, delete_models_d_yaml

    with pytest.raises(DeleteError):
        delete_models_d_yaml("../models", models_d=md)

    assert victim.exists()


# ── 缓存失效 ──────────────────────────────────────────────────────────────


def test_invalidate_all_caches_hits_scanners_and_response_cache(monkeypatch):
    """删完必须把 3 层缓存全清,否则引擎库 30s 内还显示已删的模型。"""
    called = []

    import src.api.response_cache as rc
    import src.services.model_metadata_service as mms
    import src.services.model_scanner as ms

    monkeypatch.setattr(ms, "invalidate_scan_cache", lambda: called.append("scan"))
    monkeypatch.setattr(
        mms, "invalidate_local_scan_cache", lambda: called.append("local_scan")
    )
    monkeypatch.setattr(rc, "invalidate", lambda prefix: called.append(f"rc:{prefix}"))

    from src.services.model_deleter import invalidate_all_caches

    invalidate_all_caches()

    assert set(called) == {"scan", "local_scan", "rc:engines"}


# ── 注册表清理:DB 行 ──────────────────────────────────────────────────────


async def test_clean_registry_db_removes_metadata_and_override_rows(db_session):
    from src.models.model_metadata import ModelMetadata
    from src.models.model_runtime_override import ModelRuntimeOverride

    db_session.add(ModelMetadata(engine_key="doomed", hf_id="org/doomed"))
    db_session.add(ModelMetadata(engine_key="keeper", hf_id="org/keeper"))
    db_session.add(ModelRuntimeOverride(model_id="doomed", resident=True, gpu=1))
    db_session.add(ModelRuntimeOverride(model_id="keeper", resident=True))
    await db_session.commit()

    from src.services.model_deleter import clean_registry_db

    out = await clean_registry_db(db_session, "doomed")

    assert out == {"model_metadata": True, "runtime_overrides": 1}

    from sqlalchemy import select

    keys = (await db_session.execute(select(ModelMetadata.engine_key))).scalars().all()
    ids = (
        (await db_session.execute(select(ModelRuntimeOverride.model_id)))
        .scalars()
        .all()
    )
    assert keys == ["keeper"]
    assert ids == ["keeper"]


async def test_clean_registry_db_with_no_rows_is_not_an_error(db_session):
    from src.services.model_deleter import clean_registry_db

    out = await clean_registry_db(db_session, "never_existed")

    assert out == {"model_metadata": False, "runtime_overrides": 0}


# ── 服务引用预检 ──────────────────────────────────────────────────────────


async def test_find_referencing_services_lists_model_backed_instances(db_session):
    """source_type='model' 且 source_name=<key> 的服务实例 = 软 blocker。"""
    from src.models.service_instance import ServiceInstance

    db_session.add(
        ServiceInstance(name="doomed-api", source_type="model", source_name="doomed")
    )
    db_session.add(
        ServiceInstance(name="other-api", source_type="model", source_name="keeper")
    )
    db_session.add(
        ServiceInstance(name="wf-api", source_type="workflow", source_name="doomed")
    )
    await db_session.commit()

    from src.services.model_deleter import find_referencing_services

    refs = await find_referencing_services(db_session, "doomed")

    assert [r["name"] for r in refs] == ["doomed-api"]


async def test_find_referencing_services_empty_for_unreferenced_key(db_session):
    from src.services.model_deleter import find_referencing_services

    assert await find_referencing_services(db_session, "nobody") == []


# ── 路由:/api/v1/engines/delete{,/preflight} ──────────────────────────────


@pytest.fixture
async def delete_client(tmp_path, monkeypatch, pg_engine):
    """带真 DB + 假模型树 + 「什么都没加载」的 model_manager 的 client。

    yields (client, models_root, models_d, session_factory)
    """
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.api.main import create_app
    from src.models.database import get_async_session

    models_root = tmp_path / "models"
    models_root.mkdir()
    models_d = tmp_path / "models.d"
    models_d.mkdir()
    _stub_roots(models_root, monkeypatch)

    from src.services import model_deleter as md

    monkeypatch.setattr(md, "models_d_dir", lambda: models_d)
    # 残留扫描在路由测试里桩掉:它会 git grep 真仓库,与被测行为无关且拖慢测试。
    monkeypatch.setattr(
        md, "scan_code_refs", lambda *a, **kw: {"refs": [], "truncated": False, "scan_error": None}
    )

    engine = pg_engine   # 全局只用 PostgreSQL;表由 pg_engine fixture 建好
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_session():
        async with session_factory() as session:
            yield session

    app = create_app()
    mgr = MagicMock()
    mgr.is_loaded = MagicMock(return_value=False)
    mgr._registry = MagicMock()
    mgr._registry.reload = MagicMock(return_value=0)
    mgr._registry.specs = {}
    app.state.model_manager = mgr
    app.dependency_overrides[get_async_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, models_root, models_d, session_factory, mgr

    await engine.dispose()


def _patch_scan(monkeypatch, configs: dict):
    from src.api.routes import engines as engines_route

    monkeypatch.setattr(engines_route, "scan_models", lambda: configs)


async def test_preflight_unknown_engine_returns_404(delete_client, monkeypatch):
    client, *_ = delete_client
    _patch_scan(monkeypatch, {})

    resp = await client.post("/api/v1/engines/delete/preflight", json={"name": "nope"})

    assert resp.status_code == 404
    # 必须是「未知引擎条目」而不是路由不存在的 404(app 把 HTTPException 包成
    # {"error": {"message": ...}} 信封,见 main.py 的 _http handler)
    assert "nope" in resp.json()["error"]["message"]


async def test_preflight_reports_target_size_and_registry_cleanup(
    delete_client, monkeypatch
):
    client, models_root, models_d, _sf, _mgr = delete_client
    _make_model_dir(models_root, "llm/Doomed")
    (models_d / "doomed.yaml").write_text("id: doomed\n")
    _patch_scan(monkeypatch, {"doomed": {"local_path": "llm/Doomed", "type": "llm"}})

    resp = await client.post("/api/v1/engines/delete/preflight", json={"name": "doomed"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["target_path"] == str(models_root / "llm/Doomed")
    assert body["is_dir"] is True
    assert body["size_bytes"] >= 1024
    assert body["registry_cleanup"]["models_d_yaml"].endswith("doomed.yaml")
    assert body["blockers"]["loaded"] is None
    assert body["blockers"]["services"] == []


async def test_delete_removes_dir_yaml_and_db_rows(delete_client, monkeypatch):
    client, models_root, models_d, session_factory, _mgr = delete_client
    d = _make_model_dir(models_root, "llm/Doomed")
    yaml_file = models_d / "doomed.yaml"
    yaml_file.write_text("id: doomed\n")
    _patch_scan(monkeypatch, {"doomed": {"local_path": "llm/Doomed", "type": "llm"}})

    from src.models.model_metadata import ModelMetadata
    from src.models.model_runtime_override import ModelRuntimeOverride

    async with session_factory() as s:
        s.add(ModelMetadata(engine_key="doomed", hf_id="org/doomed"))
        s.add(ModelRuntimeOverride(model_id="doomed", resident=True))
        await s.commit()

    resp = await client.post("/api/v1/engines/delete", json={"name": "doomed"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    assert body["disk_errors"] == []
    assert body["freed_bytes"] >= 1024
    assert body["registry_cleaned"] == {
        "models_d_yaml": True,
        "model_metadata": True,
        "runtime_overrides": 1,
    }
    assert not d.exists()
    assert not yaml_file.exists()

    from sqlalchemy import select

    async with session_factory() as s:
        assert (await s.execute(select(ModelMetadata.id))).first() is None
        assert (await s.execute(select(ModelRuntimeOverride.model_id))).first() is None


async def test_delete_refuses_loaded_model_even_with_force(delete_client, monkeypatch):
    """已加载 = 硬 blocker,force 不放行,磁盘必须原封不动。"""
    client, models_root, _md, _sf, mgr = delete_client
    d = _make_model_dir(models_root, "llm/Doomed")
    _patch_scan(monkeypatch, {"doomed": {"local_path": "llm/Doomed", "type": "llm"}})
    mgr.is_loaded = MagicMock(return_value=True)

    resp = await client.post(
        "/api/v1/engines/delete", json={"name": "doomed", "force": True}
    )

    assert resp.status_code == 409
    assert d.exists()


async def test_delete_refuses_service_referenced_model_without_force(
    delete_client, monkeypatch
):
    client, models_root, _md, session_factory, _mgr = delete_client
    d = _make_model_dir(models_root, "llm/Doomed")
    _patch_scan(monkeypatch, {"doomed": {"local_path": "llm/Doomed", "type": "llm"}})

    from src.models.service_instance import ServiceInstance

    async with session_factory() as s:
        s.add(
            ServiceInstance(name="doomed-api", source_type="model", source_name="doomed")
        )
        await s.commit()

    resp = await client.post("/api/v1/engines/delete", json={"name": "doomed"})

    assert resp.status_code == 409
    assert "doomed-api" in resp.text
    assert d.exists()


async def test_delete_with_force_passes_service_gate_and_keeps_the_service(
    delete_client, monkeypatch
):
    client, models_root, _md, session_factory, _mgr = delete_client
    d = _make_model_dir(models_root, "llm/Doomed")
    _patch_scan(monkeypatch, {"doomed": {"local_path": "llm/Doomed", "type": "llm"}})

    from src.models.service_instance import ServiceInstance

    async with session_factory() as s:
        s.add(
            ServiceInstance(name="doomed-api", source_type="model", source_name="doomed")
        )
        await s.commit()

    resp = await client.post(
        "/api/v1/engines/delete", json={"name": "doomed", "force": True}
    )

    assert resp.status_code == 200, resp.text
    assert not d.exists()

    from sqlalchemy import select

    async with session_factory() as s:
        names = (await s.execute(select(ServiceInstance.name))).scalars().all()
    assert names == ["doomed-api"]  # 服务本身不动


async def test_delete_rejects_target_outside_models_root(delete_client, monkeypatch):
    """component 条目塞外部绝对路径 → 400,外部文件毫发无伤。"""
    client, models_root, *_ = delete_client
    victim = models_root.parent / "victim.safetensors"
    victim.write_bytes(b"important")
    _patch_scan(monkeypatch, {})

    resp = await client.post(
        "/api/v1/engines/delete", json={"name": f"component:vae:{victim}"}
    )

    assert resp.status_code == 400
    assert victim.exists()


async def test_delete_single_component_file_skips_registry_cleanup(
    delete_client, monkeypatch
):
    """组件/LoRA 没有 engine key —— 只删文件 + 清缓存,不碰 models.d / DB。"""
    client, models_root, _md, *_ = delete_client
    d = models_root / "media" / "vae"
    d.mkdir(parents=True)
    f = d / "ae.safetensors"
    f.write_bytes(b"x" * 128)
    _patch_scan(monkeypatch, {})

    resp = await client.post(
        "/api/v1/engines/delete", json={"name": f"component:vae:{f}"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert not f.exists()
    assert body["freed_bytes"] == 128
    assert body["registry_cleaned"] == {
        "models_d_yaml": False,
        "model_metadata": False,
        "runtime_overrides": 0,
    }


async def test_delete_drops_the_just_deleted_yaml_from_code_refs(
    delete_client, monkeypatch
):
    """残留引用扫描在删除前跑,会把 models.d/<key>.yaml 自己算进去 —— 但那个文件
    紧接着就被删了。报给用户会让人去清一个已经不存在的文件。必须过滤掉。"""
    client, models_root, models_d, *_ = delete_client
    _make_model_dir(models_root, "llm/Doomed")
    yaml_file = models_d / "doomed.yaml"
    yaml_file.write_text("id: doomed\n")
    _patch_scan(monkeypatch, {"doomed": {"local_path": "llm/Doomed", "type": "llm"}})

    from src.services import model_deleter as md

    monkeypatch.setattr(
        md,
        "scan_code_refs",
        lambda *a, **kw: {
            "refs": [
                {"file": str(yaml_file), "line": 1, "text": "id: doomed"},
                {"file": "src/api/routes/services.py", "line": 377, "text": "doomed"},
            ],
            "truncated": False,
            "scan_error": None,
        },
    )

    resp = await client.post("/api/v1/engines/delete", json={"name": "doomed"})

    assert resp.status_code == 200, resp.text
    files = [r["file"] for r in resp.json()["code_refs"]]
    assert files == ["src/api/routes/services.py"]
