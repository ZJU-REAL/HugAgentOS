"""Central model configuration service (DB-driven, cached).

Replaces all os.getenv() calls for model URLs / API keys / model names.
Thread-safe snapshots validated against a transactional database revision.
Every worker observes committed changes on its next lookup without a restart.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from core.db.engine import SessionLocal
from core.db.models import ContentBlock, ModelProvider, ModelRoleAssignment
from sqlalchemy import select

logger = logging.getLogger(__name__)


def _optional_int(raw: Any) -> Optional[int]:
    """Parse an optional numeric setting; blank / unparsable means unset."""
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class ResolvedModelConfig:
    """All info needed to call one model endpoint."""

    base_url: str
    api_key: str
    model_name: str
    temperature: float = 0.6
    # None = 不限制输出长度：请求里干脆不带 max_tokens，由模型供应商自己的默认值决定。
    max_tokens: Optional[int] = None
    context_length: int = 0  # 0 = not configured; the caller falls back to a default
    timeout: int = 120
    provider: str = "openai_compatible"  # vendor/protocol, see core/llm/providers/registry.py
    # Identity of the row this came from. ``provider`` is the vendor/protocol and
    # repeats across rows; failover tracks health per configured endpoint.
    provider_id: str = ""
    provider_extra: dict = field(
        default_factory=dict
    )  # vendor-specific credentials (api_version / aws_* ...)
    extra: dict = field(default_factory=dict)


class ModelConfigService:
    """Thread-safe singleton that resolves role → model config from DB."""

    _instance: Optional["ModelConfigService"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._cache: dict[str, Optional[ResolvedModelConfig]] = {}
        self._revision: str | None = None
        self._cache_lock = threading.RLock()
        # All runtime views share one committed revision.
        self._provider_cache: dict[str, Optional[ResolvedModelConfig]] = {}
        # Failover candidate order belongs to the same snapshot.
        self._chain_cache: Optional[list[ResolvedModelConfig]] = None
        self._version: int = 0  # bumped on invalidate

    @classmethod
    def get_instance(cls) -> "ModelConfigService":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def version(self) -> int:
        self._maybe_refresh()
        return self._version

    # ── resolve ────────────────────────────────────────────────────────

    def resolve(self, role_key: str) -> Optional[ResolvedModelConfig]:
        with self._cache_lock:
            self._maybe_refresh()
            return self._cache.get(role_key)

    def resolve_provider(self, provider_id: str) -> Optional[ResolvedModelConfig]:
        with self._cache_lock:
            self._maybe_refresh()
            return self._provider_cache.get((provider_id or "").strip())

    def resolve_failover_chain(
        self, primary: Optional[ResolvedModelConfig]
    ) -> list[ResolvedModelConfig]:
        with self._cache_lock:
            self._maybe_refresh()
            chain = [
                cfg
                for cfg in self._chain_cache or []
                if primary is None or cfg.provider_id != primary.provider_id
            ]
            return ([primary] if primary else []) + chain

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._revision = None
            self._version += 1

    def _maybe_refresh(self) -> None:
        from core.db.model_config_revision import read_revision

        with self._cache_lock, SessionLocal() as db:
            revision = read_revision(db)
            if revision == self._revision:
                return
            # Read into a new snapshot, then publish under the same lock. An
            # error propagates; it must not silently authorize stale models.
            from core.db.model_config_revision import REVISION_KEY

            snapshot_revision = (
                select(ContentBlock.payload)
                .where(ContentBlock.id == REVISION_KEY)
                .scalar_subquery()
            )
            # One SELECT has one MVCC snapshot even under READ COMMITTED.
            rows = (
                db.query(ModelProvider, ModelRoleAssignment, snapshot_revision)
                .outerjoin(
                    ModelRoleAssignment,
                    ModelRoleAssignment.provider_id == ModelProvider.provider_id,
                )
                .all()
            )
            providers = list({p.provider_id: p for p, _, _ in rows if p.is_active}.values())
            resolved = {p.provider_id: self._provider_to_resolved(p) for p in providers}
            roles = {
                a.role_key: resolved[a.provider_id]
                for _, a, _ in rows
                if a is not None and a.provider_id in resolved
            }
            chat = {
                p.provider_id: resolved[p.provider_id]
                for p in providers
                if p.provider_type == "chat"
            }
            if rows:
                revision = str((rows[0][2] or {}).get("revision", ""))
            ranked = sorted(
                (p for p in providers if p.provider_type == "chat"),
                key=lambda p: (
                    -int(p.weight or 1),
                    -resolved[p.provider_id].context_length,
                    p.provider_id,
                ),
            )
            self._cache = roles
            self._provider_cache = chat
            self._chain_cache = [resolved[p.provider_id] for p in ranked]
            self._revision = revision
            self._version += 1

    @staticmethod
    def _provider_to_resolved(provider: ModelProvider) -> ResolvedModelConfig:
        from core.llm.providers.registry import get_spec, split_provider_extra

        extra = dict(provider.extra_config or {})
        ctx_len = _optional_int(extra.pop("context_length", None)) or 0
        provider_id = getattr(provider, "provider", None) or "openai_compatible"
        spec = get_spec(provider_id)
        # Separate vendor-specific credentials (api_version / deployment / aws_*) from extra into provider_extra
        provider_extra = split_provider_extra(spec, extra)
        for k in provider_extra:
            extra.pop(k, None)
        return ResolvedModelConfig(
            provider_id=str(provider.provider_id or ""),
            base_url=provider.base_url,
            api_key=provider.api_key,
            model_name=provider.model_name,
            temperature=float(extra.pop("temperature", 0.6)),
            max_tokens=_optional_int(extra.pop("max_tokens", None)),
            context_length=ctx_len,
            timeout=int(extra.pop("timeout", 120)),
            provider=provider_id,
            provider_extra=provider_extra,
            extra=extra,
        )

    # ── Context length lookup ─────────────────────────────────────────

    def get_context_length_by_model_name(self, model_name: str) -> Optional[int]:
        """Look up the context length (tokens) by model_name.

        Checks the role cache first, then falls back to scanning extra_config.context_length across all providers.
        Returns None when not configured (the caller decides its own fallback strategy).
        """
        if not model_name:
            return None
        target = model_name.strip()
        if not target:
            return None

        self._maybe_refresh()
        for cfg in self._cache.values():
            if cfg is None:
                continue
            if cfg.model_name == target and cfg.context_length > 0:
                return cfg.context_length

        # Fallback: scan the providers table directly (a provider not assigned to any role may still be referenced)
        try:
            db = SessionLocal()
            try:
                rows = (
                    db.query(ModelProvider)
                    .filter(ModelProvider.model_name == target)
                    .filter(ModelProvider.is_active == True)  # noqa: E712
                    .all()
                )
                for provider in rows:
                    extra = provider.extra_config or {}
                    raw = extra.get("context_length")
                    if raw:
                        try:
                            val = int(raw)
                            if val > 0:
                                return val
                        except (TypeError, ValueError):
                            continue
            finally:
                db.close()
        except Exception as exc:
            logger.debug("[ModelConfigService] context_length lookup failed: %s", exc)
        return None

    # ── MCP env overlay ───────────────────────────────────────────────

    def get_mcp_env_overlay(self) -> dict[str, str]:
        """Return env-var style dict for injecting into MCP sub-processes.

        Maps role configs to the legacy env var names that MCP servers expect.
        """
        overlay: dict[str, str] = {}

        main = self.resolve("main_agent")
        if main:
            overlay["MODEL_URL"] = main.base_url
            overlay["API_KEY"] = main.api_key
            overlay["BASE_MODEL_NAME"] = main.model_name
            overlay["OPENAI_API_BASE"] = main.base_url
            overlay["OPENAI_BASE_URL"] = main.base_url
            overlay["OPENAI_API_KEY"] = main.api_key

        chart = self.resolve("chart") or main
        if chart:
            overlay.setdefault("MODEL_URL", chart.base_url)
            overlay.setdefault("API_KEY", chart.api_key)
            overlay.setdefault("BASE_MODEL_NAME", chart.model_name)

        embed = self.resolve("embedding")
        if embed:
            overlay["MEM0_EMBED_URL"] = embed.base_url
            overlay["MEM0_EMBED_MODEL"] = embed.model_name
            overlay["MEM0_EMBED_API_KEY"] = embed.api_key
            dims = embed.extra.get("dimensions")
            if dims:
                overlay["MEM0_EMBED_DIMS"] = str(dims)

        reranker = self.resolve("reranker")
        if reranker:
            overlay["RERANKER_URL"] = reranker.base_url
            overlay["RERANKER_MODEL"] = reranker.model_name
            overlay["RERANKER_API_KEY"] = reranker.api_key

        return overlay
