"""Central model configuration service (DB-driven, cached).

Replaces all os.getenv() calls for model URLs / API keys / model names.
Thread-safe snapshots validated against a transactional database revision.
Every worker observes committed changes on its next lookup without a restart.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
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
    timeout: int = 600
    provider: str = "openai_compatible"  # vendor/protocol, see core/llm/providers/registry.py
    # Identity of the row this came from. ``provider`` is the vendor/protocol and
    # repeats across rows; failover tracks health per configured endpoint.
    provider_id: str = ""
    provider_extra: dict = field(
        default_factory=dict
    )  # vendor-specific credentials (api_version / aws_* ...)
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _Snapshot:
    """One committed model configuration, published as a whole and never mutated.

    ``revision`` is the database revision the rows were read under; ``None``
    marks a snapshot that must not be trusted (nothing loaded yet, or a forced
    reload).
    """

    revision: Optional[str]
    version: int
    roles: dict[str, ResolvedModelConfig] = field(default_factory=dict)
    chat_providers: dict[str, ResolvedModelConfig] = field(default_factory=dict)
    providers: dict[str, ResolvedModelConfig] = field(default_factory=dict)
    chain: tuple[ResolvedModelConfig, ...] = ()


class ModelConfigService:
    """Thread-safe singleton that resolves role → model config from DB.

    Every lookup compares the committed revision (one primary-key read) with the
    snapshot it holds, so a change committed by any worker or process is seen
    on the next lookup. Lookups never wait on each other: the snapshot is
    swapped atomically, and only a revision mismatch takes the refresh lock,
    which one thread holds while the others that saw the same mismatch wait for
    its result instead of reading the tables again.
    """

    _instance: Optional["ModelConfigService"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._snapshot = _Snapshot(revision=None, version=0)
        self._refresh_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "ModelConfigService":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def version(self) -> int:
        return self._current().version

    # ── resolve ────────────────────────────────────────────────────────

    def resolve(self, role_key: str) -> Optional[ResolvedModelConfig]:
        return self._current().roles.get(role_key)

    def resolve_provider(self, provider_id: str) -> Optional[ResolvedModelConfig]:
        return self._current().chat_providers.get((provider_id or "").strip())

    def resolve_failover_chain(
        self, primary: Optional[ResolvedModelConfig]
    ) -> list[ResolvedModelConfig]:
        chain = [
            cfg
            for cfg in self._current().chain
            if primary is None or cfg.provider_id != primary.provider_id
        ]
        return ([primary] if primary else []) + chain

    def invalidate_cache(self) -> None:
        """Force the next lookup to reload, for rows written without a revision bump."""
        # Under the refresh lock, so a reload already reading the old rows
        # cannot publish them over this request.
        with self._refresh_lock:
            self._snapshot = replace(self._snapshot, revision=None)

    def _current(self) -> _Snapshot:
        from core.db.model_config_revision import read_revision

        snapshot = self._snapshot
        observed = None
        if snapshot.revision is not None:
            with SessionLocal() as db:
                observed = read_revision(db)
            if observed == snapshot.revision:
                return snapshot
        with self._refresh_lock:
            # A reload finished while this thread waited. It is current only if
            # it carries the revision this lookup observed after it began.
            current = self._snapshot
            if observed is not None and current.revision == observed:
                return current
            # An error propagates; it must not silently authorize stale models.
            self._snapshot = self._load(current.version + 1)
            return self._snapshot

    def _load(self, version: int) -> _Snapshot:
        from core.db.model_config_revision import REVISION_KEY

        snapshot_revision = (
            select(ContentBlock.payload).where(ContentBlock.id == REVISION_KEY).scalar_subquery()
        )
        with SessionLocal() as db:
            # One SELECT has one MVCC snapshot even under READ COMMITTED, so the
            # revision it carries is exactly the one these rows belong to.
            rows = (
                db.query(ModelProvider, ModelRoleAssignment, snapshot_revision)
                .outerjoin(
                    ModelRoleAssignment,
                    ModelRoleAssignment.provider_id == ModelProvider.provider_id,
                )
                .all()
            )
            if rows:
                revision = str((rows[0][2] or {}).get("revision", ""))
            else:
                from core.db.model_config_revision import read_revision

                revision = read_revision(db)
        providers = list({p.provider_id: p for p, _, _ in rows if p.is_active}.values())
        resolved = {p.provider_id: self._provider_to_resolved(p) for p in providers}
        chat = [p for p in providers if p.provider_type == "chat"]
        ranked = sorted(
            chat,
            key=lambda p: (
                -int(p.weight or 1),
                -resolved[p.provider_id].context_length,
                p.provider_id,
            ),
        )
        return _Snapshot(
            revision=revision,
            version=version,
            roles={
                a.role_key: resolved[a.provider_id]
                for _, a, _ in rows
                if a is not None and a.provider_id in resolved
            },
            chat_providers={p.provider_id: resolved[p.provider_id] for p in chat},
            providers=resolved,
            chain=tuple(resolved[p.provider_id] for p in ranked),
        )

    @staticmethod
    def _provider_to_resolved(provider: ModelProvider) -> ResolvedModelConfig:
        from core.models.providers.registry import get_spec, split_provider_extra

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
            timeout=int(extra.pop("timeout", 600)),
            provider=provider_id,
            provider_extra=provider_extra,
            extra=extra,
        )

    # ── Context length lookup ─────────────────────────────────────────

    def get_context_length_by_model_name(self, model_name: str) -> Optional[int]:
        """Look up the context length (tokens) by model_name.

        Role assignments win; any other active provider with that model name
        (one not assigned to a role may still be referenced) is the fallback.
        Returns None when not configured (the caller decides its own fallback strategy).
        """
        target = (model_name or "").strip()
        if not target:
            return None
        snapshot = self._current()
        for cfg in (*snapshot.roles.values(), *snapshot.providers.values()):
            if cfg.model_name == target and cfg.context_length > 0:
                return cfg.context_length
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
