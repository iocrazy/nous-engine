"""Fetch and cache model metadata from ModelScope / HuggingFace."""

import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings, load_model_configs
from src.models.model_metadata import ModelMetadata

logger = logging.getLogger(__name__)

MODELSCOPE_API = "https://modelscope.cn/api/v1/models"
HF_API = "https://huggingface.co/api/models"


def _format_size(size_bytes: int | None) -> str | None:
    if size_bytes is None:
        return None
    gb = size_bytes / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.2f}GB"
    mb = size_bytes / (1024 ** 2)
    return f"{mb:.0f}MB"


async def _fetch_modelscope(client: httpx.AsyncClient, repo_id: str) -> dict | None:
    """Fetch metadata from ModelScope API."""
    try:
        resp = await client.get(f"{MODELSCOPE_API}/{repo_id}", timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json().get("Data", {})
        model_infos = data.get("ModelInfos", {})
        # Extract model size from safetensor or first available key
        model_size = None
        for info in model_infos.values():
            if "model_size" in info:
                model_size = info["model_size"]
                break
        tensor_types = None
        for info in model_infos.values():
            if "tensor_type" in info:
                tensor_types = info["tensor_type"]
                break

        org = data.get("Organization", {})
        return {
            "organization": org.get("Name") if isinstance(org, dict) else None,
            "model_size_bytes": model_size,
            "frameworks": data.get("Frameworks"),
            "libraries": data.get("Libraries"),
            "license": data.get("License") or None,
            "languages": None,  # ModelScope doesn't have a standard languages field
            "tags": data.get("Tags"),
            "tensor_types": tensor_types,
            "description": data.get("ChineseName"),
        }
    except Exception as e:
        logger.warning("ModelScope fetch failed for %s: %s", repo_id, e)
        return None


async def _fetch_huggingface(client: httpx.AsyncClient, repo_id: str) -> dict | None:
    """Fetch metadata from HuggingFace API."""
    try:
        resp = await client.get(f"{HF_API}/{repo_id}", timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        tags = data.get("tags", [])
        # Extract structured info from tags
        frameworks = [t for t in tags if t in ("pytorch", "safetensors", "onnx", "tensorflow", "jax", "transformers")]
        license_tag = next((t.replace("license:", "") for t in tags if t.startswith("license:")), None)
        lang_tags = [t for t in tags if len(t) == 2 and t.isalpha()]
        card = data.get("cardData") or {}

        return {
            "organization": data.get("author"),
            "model_size_bytes": data.get("usedStorage"),
            "frameworks": frameworks or None,
            "libraries": None,
            "license": license_tag or card.get("license"),
            "languages": card.get("language") or lang_tags or None,
            "tags": [t for t in tags if t not in frameworks and not t.startswith("license:") and len(t) > 2],
            "tensor_types": None,
            "description": None,
        }
    except Exception as e:
        logger.warning("HuggingFace fetch failed for %s: %s", repo_id, e)
        return None


async def fetch_and_store(session: AsyncSession, engine_key: str, cfg: dict) -> ModelMetadata | None:
    """Fetch metadata for one engine and store in DB. Prefer ModelScope, fallback HF."""
    ms_id = cfg.get("modelscope_id")
    hf_id = cfg.get("hf_id")
    if not ms_id and not hf_id:
        return None

    async with httpx.AsyncClient() as client:
        meta = None
        if ms_id:
            meta = await _fetch_modelscope(client, ms_id)
        if meta is None and hf_id:
            meta = await _fetch_huggingface(client, hf_id)

    if meta is None:
        # round9 BUG2:远端非 200 / 网络抖动返 None —— 不动 DB。
        # 旧实现里 refresh_metadata 已先把好行删了再走到这,一次抖动 = 永久丢元数据。
        # 现在 fetch 失败直接放手保留旧行(refresh 也不再预删)。
        return None

    fields = {
        "modelscope_id": ms_id,
        "hf_id": hf_id,
        "fetched_at": datetime.now(timezone.utc),
        **meta,
    }
    # round9 BUG3 upsert:fetch_and_store 旧实现恒 add(无 select-before-insert),
    # 两个并发 /sync-metadata 都 add 同 engine_key → 第二个撞 unique → IntegrityError 500。
    # 改 select→update,缺则 insert;insert 撞并发 unique 时 rollback 回退为 update。
    existing = (
        await session.execute(
            select(ModelMetadata).where(ModelMetadata.engine_key == engine_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        for k, v in fields.items():
            setattr(existing, k, v)
        await session.commit()
        await session.refresh(existing)
        return existing

    row = ModelMetadata(engine_key=engine_key, **fields)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        # 并发 insert 抢先了同 engine_key —— 回退取它再覆盖更新。
        await session.rollback()
        existing = (
            await session.execute(
                select(ModelMetadata).where(ModelMetadata.engine_key == engine_key)
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        for k, v in fields.items():
            setattr(existing, k, v)
        await session.commit()
        await session.refresh(existing)
        return existing
    await session.refresh(row)
    return row


async def get_all_metadata(session: AsyncSession) -> dict[str, ModelMetadata]:
    """Return all cached metadata keyed by engine_key."""
    result = await session.execute(select(ModelMetadata))
    return {row.engine_key: row for row in result.scalars().all()}


async def sync_metadata(session: AsyncSession) -> dict[str, ModelMetadata]:
    """Check configs, fetch metadata for any engine not yet in DB."""
    configs = load_model_configs()
    existing = await get_all_metadata(session)
    for key, cfg in configs.items():
        if key not in existing:
            row = await fetch_and_store(session, key, cfg)
            if row:
                existing[key] = row
    return existing


async def refresh_metadata(session: AsyncSession, engine_key: str) -> ModelMetadata | None:
    """Force re-fetch metadata for a specific engine."""
    configs = load_model_configs()
    cfg = configs.get(engine_key)
    if not cfg:
        return None
    # round9 BUG2:不再「先删后 fetch」—— fetch_and_store 现在是 upsert,
    # fetch 成功才覆盖、失败返 None 保留旧行。网络抖动不会再清掉缓存。
    return await fetch_and_store(session, engine_key, cfg)


# scan_local_models 的 30s TTL 缓存(性能 P1):原每次调用全走盘(nested iterdir),
# /engines 冷 miss 阻塞(实测 859ms 里的一部分)。照 model_scanner._SCAN_CACHE 口径缓存。
# base 变了(LOCAL_MODELS_PATH 改)自动失效;/scan 端点显式 invalidate_local_scan_cache。
_LOCAL_SCAN_CACHE: dict = {"data": None, "ts": 0.0, "base": None}
_LOCAL_SCAN_TTL_SECONDS = 30


def invalidate_local_scan_cache() -> None:
    """清 scan_local_models 缓存 —— /scan 端点或模型目录变更后调用,强制下次重扫。"""
    _LOCAL_SCAN_CACHE["data"] = None
    _LOCAL_SCAN_CACHE["ts"] = 0.0
    _LOCAL_SCAN_CACHE["base"] = None


def scan_local_models() -> set[str]:
    """scan_local_models 的缓存包装(30s TTL)。见 _scan_local_models_uncached。"""
    import time
    settings = get_settings()
    base = str(settings.LOCAL_MODELS_PATH)
    now = time.monotonic()
    cached = _LOCAL_SCAN_CACHE["data"]
    if (
        cached is not None
        and _LOCAL_SCAN_CACHE["base"] == base
        and now - _LOCAL_SCAN_CACHE["ts"] < _LOCAL_SCAN_TTL_SECONDS
    ):
        return cached
    result = _scan_local_models_uncached()
    _LOCAL_SCAN_CACHE["data"] = result
    _LOCAL_SCAN_CACHE["ts"] = now
    _LOCAL_SCAN_CACHE["base"] = base
    return result


def _scan_local_models_uncached() -> set[str]:
    """Scan LOCAL_MODELS_PATH and return set of local_path dirs that exist.

    Layout:
      llm/<MODEL>                          — depth 2
      tts/<MODEL>                          — depth 2
      embedding/<MODEL>                    — depth 2(2026-09-11 从
                                             text/embedding/ 提成顶层桶)
      media/<sub>/<MODEL>                  — depth 3, diffusers models and
                                             component buckets
    """
    settings = get_settings()
    base = Path(settings.LOCAL_MODELS_PATH)
    if not base.exists():
        return set()
    found = set()
    for type_dir in base.iterdir():
        if not type_dir.is_dir():
            continue
        if type_dir.name == "media":
            for child in type_dir.iterdir():
                if not child.is_dir():
                    continue
                for model_dir in child.iterdir():
                    if model_dir.is_dir():
                        found.add(f"{type_dir.name}/{child.name}/{model_dir.name}")
            continue
        for model_dir in type_dir.iterdir():
            if model_dir.is_dir():
                found.add(f"{type_dir.name}/{model_dir.name}")
    return found
