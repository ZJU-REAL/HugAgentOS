"""Context items and their provenance metadata."""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional
from core.infra.immutable import FrozenDict, freeze_json, thaw_json
from core.harness.execution_manifest import stable_hash
from core.llm.context.content import estimate_context_tokens, normalize_context_content
from core.llm.context import ir_constants

@dataclass(frozen=True)
class ContextItem:
    item_id: str
    kind: str
    origin: str
    trust: str
    visibility: str
    priority: int
    token_budget: int
    truncation_policy: str
    content_ref: str
    content_hash: str
    cache_class: str
    created_seq: int
    token_estimate: int
    render_role: str = "user"
    render_name: str = ""
    pair_id: str = ""
    message_group: str = ""
    unit_id: str = ""
    content: Any = field(default=None, repr=False, compare=False)
    metadata: Mapping[str, Any] = field(
        default_factory=FrozenDict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", freeze_json(self.content))
        object.__setattr__(self, "metadata", freeze_json(self.metadata or {}))

    @classmethod
    def create(
        cls,
        *,
        item_id: str,
        kind: str,
        origin: str,
        trust: str,
        visibility: str,
        priority: int,
        token_budget: int,
        truncation_policy: str,
        content: Any,
        cache_class: str,
        created_seq: int,
        render_role: str = "user",
        pair_id: str = "",
        message_group: str = "",
        unit_id: str = "",
        content_ref: Optional[str] = None,
        content_hash: Optional[str] = None,
        token_estimate: Optional[int] = None,
        render_name: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "ContextItem":
        normalized_content = normalize_context_content(content)
        if (
            isinstance(normalized_content, Mapping)
            and str(normalized_content.get("type") or "") == "data"
        ):
            truncation_policy = ir_constants.POLICY_DROP
        computed_hash = stable_hash(normalized_content)
        supplied_hash = str(content_hash or "")
        external_reference = normalized_content is None and bool(supplied_hash)
        digest = supplied_hash if external_reference else computed_hash
        estimate = (
            max(0, int(token_estimate or 0))
            if external_reference
            else estimate_context_tokens(normalized_content)
        )
        reference = (
            str(content_ref)
            if content_ref is not None
            and (external_reference or supplied_hash == computed_hash)
            else f"sha256:{digest}"
        )
        return cls(
            item_id=str(item_id),
            kind=str(kind),
            origin=str(origin),
            trust=str(trust),
            visibility=str(visibility),
            priority=int(priority),
            token_budget=max(0, int(token_budget)),
            truncation_policy=str(truncation_policy),
            content_ref=reference,
            content_hash=digest,
            cache_class=str(cache_class),
            created_seq=int(created_seq),
            token_estimate=estimate,
            render_role=str(render_role),
            render_name=str(render_name or ""),
            pair_id=str(pair_id or ""),
            message_group=str(message_group or f"item:{item_id}"),
            unit_id=str(unit_id or f"item:{item_id}"),
            content=normalized_content,
            metadata=metadata or {},
        )

    def with_content(self, content: Any) -> "ContextItem":
        normalized_content = normalize_context_content(content)
        digest = stable_hash(normalized_content)
        return replace(
            self,
            content=freeze_json(normalized_content),
            content_hash=digest,
            content_ref=f"sha256:{digest}",
            token_estimate=estimate_context_tokens(normalized_content),
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "origin": self.origin,
            "trust": self.trust,
            "visibility": self.visibility,
            "priority": self.priority,
            "token_budget": self.token_budget,
            "truncation_policy": self.truncation_policy,
            "content_ref": self.content_ref,
            "content_hash": self.content_hash,
            "cache_class": self.cache_class,
            "created_seq": self.created_seq,
            "token_estimate": self.token_estimate,
            "render_role": self.render_role,
            "render_name_hash": stable_hash(self.render_name),
            "pair_id": self.pair_id or None,
            "message_group": self.message_group,
            "unit_id": self.unit_id,
        }


def make_text_context_item(
    text: str,
    *,
    item_id: str,
    kind: str,
    origin: str,
    trust: str,
    created_seq: int,
    priority: int = 700,
    token_budget: int = 4_000,
    truncation_policy: str = ir_constants.POLICY_HEAD_TAIL,
    render_role: str = "user",
    cache_class: str = "dynamic",
) -> ContextItem:
    """Create a framework-neutral, explicitly-provenanced text item."""
    return ContextItem.create(
        item_id=item_id,
        kind=kind,
        origin=origin,
        trust=trust,
        visibility=ir_constants.VISIBILITY_MODEL,
        priority=priority,
        token_budget=token_budget,
        truncation_policy=truncation_policy,
        content=str(text or ""),
        cache_class=cache_class,
        created_seq=created_seq,
        render_role=render_role,
        render_name=render_role,
        message_group=item_id,
    )


def session_context_metadata(item: ContextItem) -> dict[str, Any]:
    """Carry provenance on a positional session row without stale identity/sequence."""
    payload = item.to_manifest()
    for key in (
        "item_id",
        "created_seq",
        "message_group",
        "unit_id",
        "content_ref",
        "content_hash",
        "token_estimate",
        "render_name_hash",
    ):
        payload.pop(key, None)
    return payload


@dataclass(frozen=True)
class ContextAssembly:
    included: tuple[ContextItem, ...]
    excluded: tuple[ContextItem, ...]
    _manifest: Mapping[str, Any] = field(repr=False)
    manifest_hash: str
    used_tokens: int
    total_budget: int
    over_budget: bool = False
    cut_units: int = 0
    pruned_items: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "included", tuple(self.included))
        object.__setattr__(self, "excluded", tuple(self.excluded))
        object.__setattr__(self, "_manifest", freeze_json(self._manifest))

    @property
    def manifest(self) -> dict[str, Any]:
        """Return a detached JSON-serializable copy of the audit manifest."""
        manifest = thaw_json(self._manifest)
        if not isinstance(manifest, dict):  # pragma: no cover - frozen in __post_init__
            raise TypeError("context manifest must thaw to a dictionary")
        return manifest
