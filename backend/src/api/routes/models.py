"""GET /api/v1/models — raw scanner output.

Sibling to /api/v1/engines. Where /engines returns *loadable* yaml-curated
entries enriched with load_status + metadata, /models returns the unfiltered
scanner walk: yaml entries + any auto-detected dirs on disk, with their
preset `files{}` block when declared.

本端点是"磁盘上有哪些模型"的原始视图(含自动发现的目录)。
"""
from __future__ import annotations

from fastapi import APIRouter

from src.api.response_cache import cached
from src.services.model_scanner import scan_models

router = APIRouter(prefix="/api/v1/models", tags=["models"])


@router.get("")
@cached("models", ttl=30)
async def list_models():
    """Return every model the scanner sees.

    Response shape: ``{"models": [{...}, ...]}`` keyed list (NOT a dict),
    sorted by id for stable ETags. The cache is invalidated alongside
    /engines on /engines/scan and similar mutating endpoints — see
    `main.py:_invalidate("models", "engines")`.
    """
    configs = scan_models()
    items = [{"id": k, **v} for k, v in sorted(configs.items())]
    return {"models": items}
