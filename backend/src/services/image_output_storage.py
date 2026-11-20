"""Image output storage with signed URLs.

write_image(png_bytes, ext='png') →
    {uuid, path, url}        when ADMIN_SESSION_SECRET is configured
    {uuid, path, url=None}   in dev mode (no secret) — caller should fall
                             back to base64 inline so tests / fresh installs
                             still work end-to-end.

URL shape:
    /files/images/{YYYY-MM-DD}/{uuid}.{ext}?token={hmac}&expires={unix_ts}

The HMAC binds (uuid, expires) so a token never grants access to a
different file or a different time window. Default TTL is 1h; admin can
override per-image via the optional `ttl_seconds` arg (DiffusersImageBackend
plumbs spec.params['url_ttl_seconds'] through).

ADMIN_SESSION_SECRET is reused as the signing key — it's already required
for the admin session cookie HMAC, so no new secret to manage. The dev
fallback (no secret → no URL) keeps tests + fresh installs working without
forcing operators to configure crypto on day one.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

from src.config import get_settings
from src.services.comfy.outputs import SERVABLE_EXTS

logger = logging.getLogger(__name__)


def _outputs_root() -> Path:
    """Image outputs 落盘根目录。优先级(从高到低):

    1. **`$NOUS_IMAGE_OUTPUTS`** — 显式 override(per-process,测试 / 临时切目录用)
    2. **`Settings.NAS_OUTPUTS_PATH/images`** — 项目级 .env 配置(跟 `NAS_MODELS_PATH`
       一对,默认 `/mnt/nas/outputs/images`;本机部署常改 `/media/heygo/program/.../outputs`)
    3. **`~/.gstack/outputs/images`** — 最终 fallback(零配置 dev 环境也能跑)

    设计:`.env` 改一处 `NAS_OUTPUTS_PATH`,整个项目所有产物(image/tts 音频/video/
    vision 输出)都落统一大盘 — 跟 ComfyUI 的 `OUTPUT_DIRECTORY` 配置模式一致。
    """
    override = os.environ.get("NOUS_IMAGE_OUTPUTS")
    if override:
        return Path(override).expanduser()
    nas_root = (get_settings().NAS_OUTPUTS_PATH or "").strip()
    if nas_root:
        # `/mnt/nas/outputs` → `/mnt/nas/outputs/images`(模式跟 NAS_MODELS_PATH 一致)。
        return Path(nas_root).expanduser() / "images"
    return Path.home() / ".gstack" / "outputs" / "images"


def _signing_key() -> bytes | None:
    secret = get_settings().ADMIN_SESSION_SECRET
    return secret.encode("utf-8") if secret else None


def _sign(uuid_str: str, expires: int) -> str:
    key = _signing_key()
    if key is None:
        return ""
    msg = f"{uuid_str}.{expires}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_token(uuid_str: str, expires: int, token: str) -> bool:
    """True iff token is a valid signature AND expires is in the future.

    Uses secrets.compare_digest to avoid leaking timing info on the
    hex digest comparison.
    """
    key = _signing_key()
    if key is None:
        return False
    if expires <= int(time.time()):
        return False
    expected = _sign(uuid_str, expires)
    return secrets.compare_digest(expected, token)


def write_image(png_bytes: bytes, *, ext: str = "png", ttl_seconds: int = 3600) -> dict:
    """Persist `png_bytes` under outputs/images/<date>/<uuid>.<ext>.

    Returns:
        {
          "uuid":  random uuid hex,
          "path":  absolute on-disk path (Path),
          "url":   signed URL (str) or None when no secret configured,
          "ext":   ext (without leading dot),
          "expires": unix timestamp the URL is valid until (int) or None,
        }
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    root = _outputs_root() / today
    root.mkdir(parents=True, exist_ok=True)

    file_uuid = _uuid.uuid4().hex
    path = root / f"{file_uuid}.{ext}"
    # Atomic-ish: write to .tmp then rename so partial reads can't see
    # half-flushed PNG bytes if the process crashes mid-write.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(png_bytes)
    os.replace(tmp, path)

    url: str | None = None
    expires: int | None = None
    if _signing_key() is not None:
        expires = int(time.time()) + max(60, int(ttl_seconds))
        token = _sign(file_uuid, expires)
        url = f"/files/images/{today}/{file_uuid}.{ext}?token={token}&expires={expires}"

    return {
        "uuid": file_uuid,
        "path": path,
        "url": url,
        "ext": ext,
        "expires": expires,
        "date": today,
    }


def sign_existing_image(date: str, uuid_str: str, ext: str, ttl_seconds: int = 3600) -> tuple[str | None, int | None]:
    """Re-sign a fresh URL for an already-on-disk image (L2 cache hit, spec §3.3).
    HMAC is microseconds, so a long-lived cache entry always serves a URL valid
    for the next ttl window. Returns (None, None) when no signing secret."""
    if _signing_key() is None:
        return None, None
    expires = int(time.time()) + max(60, int(ttl_seconds))
    token = _sign(uuid_str, expires)
    return f"/files/images/{date}/{uuid_str}.{ext}?token={token}&expires={expires}", expires


def resolve_path(date: str, uuid_str: str, ext: str) -> Path:
    """Build the on-disk path for a given date/uuid/ext triple.

    Used by the static route after HMAC verification; never trusts the
    caller-supplied date+uuid until verify_token has succeeded.
    """
    return _outputs_root() / date / f"{uuid_str}.{ext}"


MANAGED_MEDIA_PATH = r"/files/images/[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9a-f]{32}\.[a-z0-9]+"
_media_path = re.compile(MANAGED_MEDIA_PATH)


def managed_media_path(url: str) -> str | None:
    path = url.split("?", 1)[0]
    if _media_path.fullmatch(path) and path.rsplit(".", 1)[-1] in SERVABLE_EXTS:
        return path
    return None


def unlink_managed_media(url: str) -> bool:
    path = managed_media_path(url)
    if path is None:
        raise ValueError("invalid managed media path")
    date, filename = path.split("/")[-2:]
    try:
        directory = os.open(_outputs_root() / date, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        os.unlink(filename, dir_fd=directory)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(directory)


def reap_orphans(*, older_than_seconds: int, keep_uuids: set[str] | None = None) -> dict:
    """Reap generated images that no longer belong to anything.

    Walks every <date>/ subdir under NOUS_IMAGE_OUTPUTS. A file is deleted
    only if BOTH:
      - its mtime is past `older_than_seconds` (grace window — a just-written
        image whose ExecutionTask row hasn't committed yet looks orphan but
        is young, so the age floor protects it from the race), AND
      - `keep_uuids` is given AND the file's uuid (stem) is NOT in it.

    `keep_uuids` = image uuids still referenced by some ExecutionTask.result
    (see execution_tasks.collect_referenced_image_uuids). Passing it makes
    image lifetime = task lifetime: /history images stay browsable; only true
    orphans (failed / deleted-task leftovers) get reclaimed (spec 2026-06-09
    run-history — gallery persistence). `keep_uuids=None` → pure age reaper
    (old behavior, back-compat for callers without DB context).

    Returns {scanned, deleted, kept, dirs_pruned, errors}. Skips unrecognized ext.
    """
    root = _outputs_root()
    summary = {"scanned": 0, "deleted": 0, "kept": 0, "dirs_pruned": 0, "errors": 0}
    if not root.exists():
        return summary

    cutoff = time.time() - max(60, int(older_than_seconds))
    # image kinds + the comfy-bridge media kinds (write_media, spec §6: "保留/
    # 清理策略与 image 现状一致") — I2 fix: single-sourced from
    # services/comfy/outputs.py's SERVABLE_EXTS (same table image_files.py's
    # route whitelist derives from) so anything the bridge can produce/serve
    # is also reapable, not a separately hand-maintained list.
    allowed_ext = {f".{ext}" for ext in SERVABLE_EXTS}

    for date_dir in sorted(root.iterdir()):
        if not date_dir.is_dir():
            continue
        for f in date_dir.iterdir():
            if not f.is_file() or f.suffix.lower() not in allowed_ext:
                continue
            summary["scanned"] += 1
            # 仍被任务历史引用 → 永久保留(图寿命=任务寿命),不看 age。
            if keep_uuids is not None and f.stem in keep_uuids:
                summary["kept"] += 1
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    summary["deleted"] += 1
            except OSError as e:
                logger.warning("reap_orphans: failed to unlink %s: %s", f, e)
                summary["errors"] += 1
        # Prune the date dir if it's empty after deletion.
        try:
            if not any(date_dir.iterdir()):
                date_dir.rmdir()
                summary["dirs_pruned"] += 1
        except OSError:
            pass

    if summary["deleted"] or summary["dirs_pruned"]:
        logger.info(
            "image_output_storage.reap_orphans: scanned=%d deleted=%d dirs_pruned=%d errors=%d (cutoff=%ds)",
            summary["scanned"], summary["deleted"], summary["dirs_pruned"],
            summary["errors"], older_than_seconds,
        )
    return summary


# write_image is ext-agnostic already (caller-supplied ext, no image-specific
# encoding/validation inside) — comfy bridge (Task 6) reuses it verbatim to
# persist mp4/wav workflow outputs under the same signed-URL scheme.
write_media = write_image
