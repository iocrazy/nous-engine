"""Tests for image_output_storage + /files/images signed-URL route."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _cookie_header(admin_cookie: str) -> dict[str, str]:
    """管理端 cookie 直接拼进 Cookie header。

    httpx 0.28 起 per-request `cookies=` 已弃用(cookie 持久化语义有歧义),而
    `client` fixture 是整个模块共享的 AsyncClient —— 往实例上塞 cookie 会漏给
    同模块后续用例(比如那些故意不带 cookie、断言 403 的),所以走 header。
    """
    from src.api.admin_session import COOKIE_NAME

    return {"Cookie": f"{COOKIE_NAME}={admin_cookie}"}


@pytest.fixture
def storage_tmp(tmp_path, monkeypatch):
    """Redirect storage to a tmp dir AND restore on teardown."""
    monkeypatch.setenv("NOUS_IMAGE_OUTPUTS", str(tmp_path / "outputs"))
    yield tmp_path / "outputs"


@pytest.fixture
def with_signing_secret(monkeypatch):
    """Force ADMIN_SESSION_SECRET so the signing path runs.

    The module reads via get_settings() each call, so no further patching
    needed — but cache is per-process; just toggle directly.
    """
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "ADMIN_SESSION_SECRET", "test-secret-bytes")


@pytest.fixture
def with_login_required(monkeypatch):
    """Simulate prod-shape `ADMIN_PASSWORD` set so request_is_authed actually
    checks cookies (rather than the dev-mode `True` short-circuit).

    HMAC-rejection tests must use this — otherwise conftest's forced
    `ADMIN_PASSWORD=""` makes every browser-shape request admin-authed and the
    HMAC branch never runs (which would silently de-cover the rejection path).
    """
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", "test-password")
    monkeypatch.setattr(settings, "ADMIN_SESSION_SECRET", "test-secret-bytes")


# ----- write_image -----


def test_write_image_writes_under_dated_subdir(storage_tmp, with_signing_secret):
    from src.services.image_output_storage import write_image

    rec = write_image(b"\x89PNGFAKE", ext="png")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    expected_dir = storage_tmp / today
    assert rec["path"].parent == expected_dir
    assert rec["path"].suffix == ".png"
    assert rec["path"].read_bytes() == b"\x89PNGFAKE"
    assert rec["uuid"] == rec["path"].stem
    assert len(rec["uuid"]) == 32  # uuid4 hex


def test_write_image_no_secret_yields_null_url(storage_tmp, monkeypatch):
    """Conftest sets ADMIN_SESSION_SECRET (since p2-polish-3 the node
    rejects empty); explicitly clear here to exercise the no-signing path."""
    from src.config import get_settings
    from src.services.image_output_storage import write_image

    monkeypatch.setattr(get_settings(), "ADMIN_SESSION_SECRET", "")
    rec = write_image(b"x", ext="png")
    assert rec["url"] is None
    assert rec["expires"] is None


def test_write_image_with_secret_signs_url(storage_tmp, with_signing_secret):
    from src.services.image_output_storage import verify_token, write_image

    rec = write_image(b"x", ext="png", ttl_seconds=600)
    assert rec["url"] is not None
    assert rec["url"].startswith("/files/images/")
    assert "token=" in rec["url"] and "expires=" in rec["url"]
    assert rec["expires"] is not None
    # Round-trip verify
    token = rec["url"].split("token=")[1].split("&")[0]
    assert verify_token(rec["uuid"], rec["expires"], token)


def test_write_image_ttl_floor_is_60s(storage_tmp, with_signing_secret):
    from src.services.image_output_storage import write_image

    rec = write_image(b"x", ext="png", ttl_seconds=10)
    # max(60, 10) → expires at least 60 seconds out
    assert rec["expires"] - int(time.time()) >= 60


# ----- HMAC verify -----


def test_verify_token_rejects_tampered_uuid(storage_tmp, with_signing_secret):
    from src.services.image_output_storage import _sign, verify_token

    expires = int(time.time()) + 3600
    legit = _sign("aaaa", expires)
    assert verify_token("aaaa", expires, legit) is True
    assert verify_token("bbbb", expires, legit) is False


def test_verify_token_rejects_tampered_expires(storage_tmp, with_signing_secret):
    from src.services.image_output_storage import _sign, verify_token

    expires = int(time.time()) + 3600
    legit = _sign("aaaa", expires)
    assert verify_token("aaaa", expires + 1, legit) is False


def test_verify_token_rejects_expired(storage_tmp, with_signing_secret):
    from src.services.image_output_storage import _sign, verify_token

    expires = int(time.time()) - 1
    legit = _sign("aaaa", expires)
    assert verify_token("aaaa", expires, legit) is False


def test_verify_token_rejects_when_no_secret(storage_tmp, monkeypatch):
    from src.config import get_settings
    from src.services.image_output_storage import verify_token

    monkeypatch.setattr(get_settings(), "ADMIN_SESSION_SECRET", "")
    assert verify_token("aaaa", int(time.time()) + 3600, "anything") is False


# ----- /files/images route -----


@pytest.mark.asyncio
async def test_image_route_serves_valid_signed_url(storage_tmp, with_signing_secret, client):
    from src.services.image_output_storage import write_image

    rec = write_image(b"\x89PNG_BYTES", ext="png", ttl_seconds=600)
    resp = await client.get(rec["url"])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"\x89PNG_BYTES"


@pytest.mark.asyncio
async def test_image_route_signed_preview_preserves_original(
    storage_tmp, with_signing_secret, with_login_required, client,
):
    from io import BytesIO

    from PIL import Image

    from src.services.image_output_storage import write_image

    buffer = BytesIO()
    Image.new("RGB", (1920, 1080), "navy").save(buffer, format="PNG")
    original = buffer.getvalue()
    rec = write_image(original, ext="png")
    url = rec["url"]

    preview = await client.get(url + "&preview=1")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/webp"
    assert Image.open(BytesIO(preview.content)).size == (384, 216)
    assert len(preview.content) < len(original)
    assert (await client.get(url)).content == original
    assert (await client.get(url.split("?")[0] + "?preview=1")).status_code == 403


@pytest.mark.asyncio
async def test_image_route_403_on_expired_token(
    storage_tmp, with_signing_secret, with_login_required, client,
):
    from src.services.image_output_storage import _sign, write_image

    rec = write_image(b"x", ext="png", ttl_seconds=600)
    expired = int(time.time()) - 10
    bad_token = _sign(rec["uuid"], expired)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"/files/images/{today}/{rec['uuid']}.png?token={bad_token}&expires={expired}"
    resp = await client.get(url)
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "url_expired"
    assert "fix" in body["error"]


@pytest.mark.asyncio
async def test_image_route_403_on_tampered_uuid(
    storage_tmp, with_signing_secret, with_login_required, client,
):
    from src.services.image_output_storage import write_image

    rec = write_image(b"x", ext="png", ttl_seconds=600)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    token = rec["url"].split("token=")[1].split("&")[0]
    expires = rec["expires"]
    other_uuid = "0" * 32
    url = f"/files/images/{today}/{other_uuid}.png?token={token}&expires={expires}"
    resp = await client.get(url)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_image_route_403_when_token_missing(with_login_required, client):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    resp = await client.get(f"/files/images/{today}/{'a' * 32}.png")
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "missing_signature"


@pytest.mark.asyncio
async def test_image_route_403_when_no_signing_secret(
    client, monkeypatch, with_login_required,
):
    """Even a 'valid'-looking URL must reject so a leaked URL from a one-off
    run with a transient secret can't survive a deploy that rotated keys."""
    from src.config import get_settings

    # Re-clear the secret AFTER with_login_required set it (fixture ordering).
    monkeypatch.setattr(get_settings(), "ADMIN_SESSION_SECRET", "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    expires = int(time.time()) + 3600
    url = f"/files/images/{today}/{'a' * 32}.png?token=deadbeef&expires={expires}"
    resp = await client.get(url)
    assert resp.status_code == 403


# ----- admin cookie auth bypass(本 fix 的核心)-----


@pytest.mark.asyncio
async def test_image_route_admin_cookie_bypasses_expired_token(
    storage_tmp, with_signing_secret, with_login_required, client,
):
    """已登录 admin 自己浏览器看自己输出图,不应被 TTL 1h 卡死。

    场景:前端 React Query 缓存里的 task URL token 是 5h 前签的(过期),
    重新 mount ImageOutputNode 直接发请求 — 应 200(cookie 路径),不该 403。
    """
    from src.api.admin_session import issue_token
    from src.services.image_output_storage import _sign, write_image

    rec = write_image(b"PNG_OWNER", ext="png", ttl_seconds=600)
    expired = int(time.time()) - 3600  # 1h ago
    stale_token = _sign(rec["uuid"], expired)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"/files/images/{today}/{rec['uuid']}.png?token={stale_token}&expires={expired}"

    admin_cookie, _ = issue_token()
    resp = await client.get(url, headers=_cookie_header(admin_cookie))
    assert resp.status_code == 200
    assert resp.content == b"PNG_OWNER"
    # 短缓存(60s),不能用 token 的 expires 锚
    assert "max-age=60" in resp.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_image_route_admin_cookie_works_without_token(
    storage_tmp, with_signing_secret, with_login_required, client,
):
    """admin cookie 路径连 token/expires query 都不需要。"""
    from src.api.admin_session import issue_token
    from src.services.image_output_storage import write_image

    rec = write_image(b"BARE", ext="png", ttl_seconds=600)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"/files/images/{today}/{rec['uuid']}.png"  # 无 query 参数

    admin_cookie, _ = issue_token()
    resp = await client.get(url, headers=_cookie_header(admin_cookie))
    assert resp.status_code == 200
    assert resp.content == b"BARE"


@pytest.mark.asyncio
async def test_image_route_admin_path_still_blocks_traversal(
    storage_tmp, with_signing_secret, with_login_required, client,
):
    """admin cookie 不能绕过 filename/path 校验 — ../ 类攻击仍 403。"""
    from src.api.admin_session import issue_token

    admin_cookie, _ = issue_token()
    # 非白名单扩展名
    resp = await client.get(
        "/files/images/2026-05-26/file.exe",
        headers=_cookie_header(admin_cookie),
    )
    assert resp.status_code == 403
    # 非法 uuid 形状
    resp = await client.get(
        "/files/images/2026-05-26/not-a-uuid.png",
        headers=_cookie_header(admin_cookie),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_image_route_400_on_bad_filename(client):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    resp = await client.get(f"/files/images/{today}/no-extension")
    assert resp.status_code == 403  # _UrlInvalidError → 403 per Stripe shape
    resp = await client.get(f"/files/images/{today}/file.exe?token=x&expires=1")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_image_route_400_on_bad_date_segment(client):
    resp = await client.get(f"/files/images/not-a-date/{'a' * 32}.png?token=x&expires=1")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_image_route_404_when_signed_but_file_missing(
    storage_tmp, with_signing_secret, client
):
    from src.services.image_output_storage import write_image

    rec = write_image(b"x", ext="png", ttl_seconds=600)
    # Delete the file but keep the (still valid) signed URL
    os.unlink(rec["path"])
    resp = await client.get(rec["url"])
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_image_route_serves_mp4_with_video_content_type(
    storage_tmp, with_signing_secret, client
):
    """comfy bridge (Task 5/6) 用 write_media(=write_image) 存 mp4;路由的
    ext 白名单 + Content-Type 推导必须覆盖 image 之外的媒体类型,不能写死
    image/*(见 image_files.py:_mime_for_ext)。"""
    from src.services.image_output_storage import write_media

    rec = write_media(b"\x00fakemp4bytes", ext="mp4", ttl_seconds=600)
    resp = await client.get(rec["url"])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content == b"\x00fakemp4bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(("ext", "content_type"), [("gif", "image/gif"), ("webm", "video/webm")])
async def test_image_route_serves_gif_and_webm(storage_tmp, with_signing_secret, client, ext, content_type):
    """I2 fix:_EXT_WHITELIST 现在从 services/comfy/outputs.py 的 SERVABLE_EXTS 单一
    来源派生——之前是手写列表,只有 png/jpg/jpeg/webp/mp4/wav,漏了 gif/webm/mov/mkv/
    mp3/flac/ogg(comfy 桥能产出的其它媒体类型经这条路由一律 403)。"""
    from src.services.image_output_storage import write_media

    rec = write_media(b"\x00fakebytes", ext=ext, ttl_seconds=600)
    resp = await client.get(rec["url"])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == content_type
    assert resp.content == b"\x00fakebytes"


def test_reap_orphans_collects_webm(storage_tmp):
    """I2 fix companion:reap 的 allowed_ext 同样从 SERVABLE_EXTS 派生——webm 是 reap
    早就手写覆盖的一个 ext,这里只是确认它仍然被派生后的集合覆盖(单一来源没有回归)。"""
    import os as _os
    from src.services.image_output_storage import reap_orphans, write_media

    webm = write_media(b"\x00fakewebm", ext="webm", ttl_seconds=60)
    two_days = time.time() - 2 * 24 * 3600
    _os.utime(webm["path"], (two_days, two_days))

    summary = reap_orphans(older_than_seconds=24 * 3600)
    assert summary["deleted"] == 1
    assert not webm["path"].exists()


# 注:write_image 的 secret→签名 URL / 无 secret→url=None 不变式由上面
# test_write_image_no_secret_yields_null_url + test_write_image_with_secret_signs_url
# 直接覆盖。收敛后 image_generate 节点已删(图像走细粒度图 + runner write_image),
# 原 ImageGenerateNode 集成测试移除(不变式无覆盖损失)。


def test_outputs_root_respects_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NOUS_IMAGE_OUTPUTS", str(tmp_path / "custom"))
    from src.services.image_output_storage import _outputs_root
    assert _outputs_root() == tmp_path / "custom"


def test_outputs_root_uses_nas_outputs_path(monkeypatch, tmp_path):
    """`Settings.NAS_OUTPUTS_PATH` 配置后 → 落 `{NAS_OUTPUTS_PATH}/images`(.env 单一开关)。"""
    monkeypatch.delenv("NOUS_IMAGE_OUTPUTS", raising=False)
    from src.config import get_settings
    monkeypatch.setattr(get_settings(), "NAS_OUTPUTS_PATH", str(tmp_path / "nas"))
    from src.services.image_output_storage import _outputs_root
    assert _outputs_root() == tmp_path / "nas" / "images"


def test_outputs_root_env_override_beats_nas_path(monkeypatch, tmp_path):
    """`$NOUS_IMAGE_OUTPUTS` 显式 override 优先于 `NAS_OUTPUTS_PATH`(测试 / 临时切目录)。"""
    monkeypatch.setenv("NOUS_IMAGE_OUTPUTS", str(tmp_path / "explicit"))
    from src.config import get_settings
    monkeypatch.setattr(get_settings(), "NAS_OUTPUTS_PATH", str(tmp_path / "nas"))
    from src.services.image_output_storage import _outputs_root
    assert _outputs_root() == tmp_path / "explicit"


def test_outputs_root_fallback_to_home_when_nas_empty(monkeypatch):
    """`NAS_OUTPUTS_PATH=""` + 无 env override → fallback `~/.gstack/outputs/images`。"""
    monkeypatch.delenv("NOUS_IMAGE_OUTPUTS", raising=False)
    from src.config import get_settings
    monkeypatch.setattr(get_settings(), "NAS_OUTPUTS_PATH", "")
    from src.services.image_output_storage import _outputs_root
    assert _outputs_root() == Path.home() / ".gstack" / "outputs" / "images"


# ----- orphan reaper -----


def test_reap_orphans_deletes_only_old_files(storage_tmp):
    """Files older than cutoff get unlinked; fresh files survive."""
    import os as _os
    from src.services.image_output_storage import reap_orphans

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bucket = storage_tmp / today
    bucket.mkdir(parents=True)
    fresh = bucket / "fresh.png"
    old = bucket / "old.png"
    fresh.write_bytes(b"\x89PNG-fresh")
    old.write_bytes(b"\x89PNG-old")
    # Backdate `old` 2 days
    two_days = time.time() - 2 * 24 * 3600
    _os.utime(old, (two_days, two_days))

    summary = reap_orphans(older_than_seconds=24 * 3600)
    assert summary["scanned"] == 2
    assert summary["deleted"] == 1
    assert fresh.exists()
    assert not old.exists()


def test_reap_orphans_prunes_empty_date_dirs(storage_tmp):
    """After deletion the empty date subdir gets rmdir'd."""
    import os as _os
    from src.services.image_output_storage import reap_orphans

    bucket = storage_tmp / "2026-04-01"
    bucket.mkdir(parents=True)
    f = bucket / "lonely.png"
    f.write_bytes(b"x")
    _os.utime(f, (time.time() - 7 * 24 * 3600, time.time() - 7 * 24 * 3600))

    summary = reap_orphans(older_than_seconds=24 * 3600)
    assert summary["deleted"] == 1
    assert summary["dirs_pruned"] == 1
    assert not bucket.exists()


def test_reap_orphans_collects_expired_media_files(storage_tmp):
    """write_media (mp4/wav write_image alias, comfy-bridge) outputs are
    reapable exactly like images — expired media must not leak disk forever
    just because reap_orphans only recognized image extensions."""
    import os as _os
    from src.services.image_output_storage import reap_orphans, write_media

    mp4 = write_media(b"\x00fakemp4", ext="mp4", ttl_seconds=60)
    wav = write_media(b"RIFFfakewav", ext="wav", ttl_seconds=60)
    two_days = time.time() - 2 * 24 * 3600
    _os.utime(mp4["path"], (two_days, two_days))
    _os.utime(wav["path"], (two_days, two_days))

    summary = reap_orphans(older_than_seconds=24 * 3600)
    assert summary["deleted"] == 2
    assert not mp4["path"].exists()
    assert not wav["path"].exists()


def test_reap_orphans_skips_non_image_files(storage_tmp):
    """Stray .md / .log files alongside don't get touched even if old."""
    import os as _os
    from src.services.image_output_storage import reap_orphans

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bucket = storage_tmp / today
    bucket.mkdir(parents=True)
    stray = bucket / "readme.md"
    stray.write_bytes(b"keep me")
    _os.utime(stray, (time.time() - 365 * 24 * 3600, time.time() - 365 * 24 * 3600))

    reap_orphans(older_than_seconds=24 * 3600)
    assert stray.exists()


def test_reap_orphans_no_root_dir_returns_zero(monkeypatch, tmp_path):
    """Outputs root never created yet → no error, summary all zeros."""
    monkeypatch.setenv("NOUS_IMAGE_OUTPUTS", str(tmp_path / "missing"))
    from src.services.image_output_storage import reap_orphans

    summary = reap_orphans(older_than_seconds=3600)
    assert summary == {"scanned": 0, "deleted": 0, "kept": 0, "dirs_pruned": 0, "errors": 0}


def test_reap_orphans_floor_60s(storage_tmp):
    """older_than_seconds < 60 still keeps files newer than 60s — never
    accidentally nuke a brand-new file by passing a 0 cutoff."""
    from src.services.image_output_storage import reap_orphans

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bucket = storage_tmp / today
    bucket.mkdir(parents=True)
    fresh = bucket / "just-written.png"
    fresh.write_bytes(b"x")
    # mtime = now (newer than the 60s floor)

    summary = reap_orphans(older_than_seconds=0)  # operator passed 0
    assert summary["deleted"] == 0
    assert fresh.exists()


def test_reap_orphans_keeps_referenced_uuids(storage_tmp):
    """keep_uuids:被任务历史引用的图永久保留(图寿命=任务寿命),只清真 orphan。"""
    import os as _os
    from src.services.image_output_storage import reap_orphans

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bucket = storage_tmp / today
    bucket.mkdir(parents=True)
    kept = bucket / "keepme.png"
    orphan = bucket / "orphan.png"
    kept.write_bytes(b"k")
    orphan.write_bytes(b"o")
    # 两个都 backdate 2 天(都过 age cutoff)
    old = time.time() - 2 * 24 * 3600
    _os.utime(kept, (old, old))
    _os.utime(orphan, (old, old))

    summary = reap_orphans(older_than_seconds=24 * 3600, keep_uuids={"keepme"})
    assert kept.exists()        # 被引用 → 不删,哪怕过期
    assert not orphan.exists()  # 无引用 + 过期 → 删
    assert summary["kept"] == 1
    assert summary["deleted"] == 1


def test_write_image_returns_date(tmp_path, monkeypatch):
    monkeypatch.setenv("NOUS_IMAGE_OUTPUTS", str(tmp_path))
    import datetime as _dt
    from src.services import image_output_storage as ios
    rec = ios.write_image(b"\x89PNG\r\n", ext="png", ttl_seconds=3600)
    assert rec["date"] == _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def test_sign_existing_image_roundtrips_token(tmp_path, monkeypatch):
    monkeypatch.setenv("NOUS_IMAGE_OUTPUTS", str(tmp_path))
    from src.config import get_settings
    monkeypatch.setattr(get_settings(), "ADMIN_SESSION_SECRET", "sek")
    from src.services import image_output_storage as ios
    url, expires = ios.sign_existing_image("2026-05-20", "abc123", "png", ttl_seconds=3600)
    assert url is not None and "/files/images/2026-05-20/abc123.png?token=" in url
    tok = url.split("token=", 1)[1].split("&", 1)[0]
    assert ios.verify_token("abc123", expires, tok)


def test_sign_existing_image_no_secret_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("NOUS_IMAGE_OUTPUTS", str(tmp_path))
    from src.config import get_settings
    monkeypatch.setattr(get_settings(), "ADMIN_SESSION_SECRET", "")
    from src.services import image_output_storage as ios
    url, expires = ios.sign_existing_image("2026-05-20", "abc", "png")
    assert url is None and expires is None
