"""MemoryProvider ABC + shared PluginBase.

Contract (see spec 2026-04-16-wave1-platform-contracts-design.md 决策 3):
- fail-fast:  initialize(), ClientError subclasses → MUST raise
- best-effort: add_entries/prefetch → should swallow InternalError, log
- critical:    ContextEngine.compress (in context/base.py) → raise ContextOverflowError
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypedDict


# ---------- Exceptions ---------- #

class MemoryProviderError(Exception):
    """Base class for all MemoryProvider errors."""


class MemoryProviderClientError(MemoryProviderError):
    """Caller did something wrong (bad input, auth, oversized). MUST be raised."""


class MemoryProviderInternalError(MemoryProviderError):
    """Transient infra failure (DB hiccup). Callers' choice to retry;
    best-effort methods swallow and log."""


# ---------- Types ---------- #

class MemoryEntry(TypedDict):
    category: str         # 'preference' | 'fact' | 'instruction' | 'custom'
    content: str          # max 10KB per entry (enforced by implementations)
    context_key: str | None


class StoredMemoryEntry(MemoryEntry):
    id: int
    api_key_id: int | None
    created_at: str  # ISO8601


# ---------- PluginBase (shared by MemoryProvider + ContextEngine) ---------- #

class PluginBase(ABC):
    """Shared lifecycle for MemoryProvider & ContextEngine.

    Implementations:
    - Override initialize() (required; fail-fast)
    - Override shutdown() (optional; best-effort)
    - Override system_prompt_block() (optional; defaults to "")
    """

    @abstractmethod
    async def initialize(self) -> None:
        """fail-fast: raise if cannot start. Called at app startup."""

    async def shutdown(self) -> None:
        """best-effort: cleanup resources. Log errors, don't raise."""

    async def system_prompt_block(self, *, instance_id: int) -> str:
        """Static text or dynamically fetched. Default empty string.

        MUST return quickly (<50ms); do not block on LLM calls here.
        """
        return ""


# ---------- MemoryProvider ABC ---------- #

class MemoryProvider(PluginBase):
    """ABC for long-term memory storage/retrieval.

    nous-engine 只存不抽取（决策 13）：mediahub 等上层 app 负责
    "这段对话里什么值得记"的逻辑，然后调 add_entries 传结构化条目。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g. 'pg', 'redis'). Used in logs/config."""

    @abstractmethod
    async def add_entries(
        self,
        *,
        owner_key_id: int,
        entries: list[MemoryEntry],
        context_key: str | None = None,
    ) -> list[int]:
        """Store entries owned by the caller's API key; return new entry ids.

        legacy rip:memory 按调用方 API key 切作用域(M:N 无单一 instance,谁存谁拥有)。

        Contract:
        - entries == []: return []; no DB write; no raise (idempotent).
        - len(entries) > 100: raise MemoryProviderClientError.
        - any content > 10KB: raise MemoryProviderClientError.
        - DB transient failure: raise MemoryProviderInternalError
          (caller may swallow; HTTP layer maps to 500).
        """

    @abstractmethod
    async def prefetch(
        self,
        *,
        owner_key_id: int,
        query: str,
        limit: int = 10,
        context_key: str | None = None,
    ) -> list[StoredMemoryEntry]:
        """best-effort: recall memories owned by the caller's API key.

        Returns [] on InternalError (logs warning).
        """

    async def on_session_end(
        self, *, instance_id: int, turns: list[dict]
    ) -> None:
        """Optional: post-session hook. Default no-op.

        Implementations MAY extract facts here. nous-engine's built-in
        PGMemoryProvider does NOT (see 决策 2B — 只存不抽取).
        """

    async def on_pre_compress(
        self, *, instance_id: int, messages: list[dict]
    ) -> str | None:
        """Optional: extract summary before ContextEngine compression.

        Return summary string or None. Default no-op.
        """
        return None
