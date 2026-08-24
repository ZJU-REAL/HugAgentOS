"""Framework-neutral canonical context IR and deterministic budget assembler.

This module deliberately has no AgentScope dependency.  It owns provenance,
trust, selection, truncation and the sanitized inclusion/exclusion manifest;
``context_adapter`` is the only layer that translates these items to model SDK
messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

from core.immutable import FrozenDict, freeze_json, thaw_json
from core.llm.execution_manifest import canonical_json, stable_hash

CONTEXT_SCHEMA_VERSION = "harness.context.v1"
SESSION_CONTEXT_META_KEY = "_context_item"
CONTEXT_SEQUENCE_STRIDE = 1_000

KIND_SYSTEM_RULE = "system_rule"
KIND_USER_INPUT = "user_input"
KIND_ASSISTANT = "assistant_history"
KIND_MEMORY = "memory"
KIND_IDENTITY = "identity"
KIND_PROJECT = "project_material"
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"
KIND_REMINDER = "reminder"
KIND_COMPACTION = "compaction_summary"
KIND_ATTACHMENT = "attachment"
KIND_STEER = "steer"
KIND_REFERENCE = "reference"

POLICY_NEVER = "never"
POLICY_DROP = "drop"
POLICY_HEAD_TAIL = "head_tail"
POLICY_TAIL = "tail"

VISIBILITY_MODEL = "model"
VISIBILITY_MANIFEST_ONLY = "manifest_only"

_SYSTEM_KINDS = {KIND_SYSTEM_RULE, KIND_PROJECT, KIND_REFERENCE}
_TOOL_KINDS = {KIND_TOOL_CALL, KIND_TOOL_RESULT}
_TRUST_RANK = {
    "platform": 0,
    "admin": 1,
    "system": 2,
    "memory": 3,
    "tool": 4,
    "assistant": 5,
    "user": 6,
    "external": 7,
    "untrusted": 8,
}


class ContextAdapterProtocol(Protocol):
    """Framework-neutral seam for turning transport rows into canonical IR."""

    def items_from_messages(
        self,
        messages: Sequence[Any],
        *,
        summary_text: Any = None,
        promote_latest_user: bool = True,
    ) -> list["ContextItem"]: ...

    def messages_from_items(self, items: Iterable["ContextItem"]) -> list[Any]: ...

    def reference_items_from_execution_manifest(
        self, manifest: Any
    ) -> list["ContextItem"]: ...

    def items_from_provider_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> list["ContextItem"]: ...

    def provider_messages_from_items(
        self,
        items: Iterable["ContextItem"],
    ) -> list[Mapping[str, Any]]: ...


def _normalize_context_content(content: Any) -> Any:
    """Convert SDK block objects into stable, framework-neutral JSON values."""
    if hasattr(content, "model_dump"):
        return _normalize_context_content(content.model_dump(mode="json"))
    if isinstance(content, Mapping):
        return {
            str(key): _normalize_context_content(value)
            for key, value in content.items()
        }
    if isinstance(content, (list, tuple)):
        return [_normalize_context_content(value) for value in content]
    return content


def estimate_context_tokens(content: Any) -> int:
    """Deterministic conservative estimate without provider-specific imports."""
    content = _normalize_context_content(content)
    if content is None:
        return 0
    if isinstance(content, str):
        return max(1, (len(content.encode("utf-8")) + 3) // 4) if content else 0
    if isinstance(content, Mapping) and content.get("type") == "data":
        # Provider image accounting is not proportional to base64 bytes. Keep a
        # conservative fixed reserve without letting transport encoding evict
        # the actual attachment before the provider can count it precisely.
        return 1_024
    if isinstance(content, list):
        return sum(estimate_context_tokens(item) for item in content)
    return max(1, (len(canonical_json(content).encode("utf-8")) + 3) // 4)


def _truncate_text(text: str, max_tokens: int, *, tail_only: bool = False) -> str:
    if not text or max_tokens <= 0:
        return ""
    max_bytes = max(4, max_tokens * 4)
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    # Character slicing is deterministic and safe for Unicode. Iterate down if
    # multi-byte text still exceeds the byte reserve.
    max_chars = max(1, max_bytes)
    head = 0
    tail = 0
    marker = ""
    if tail_only:
        tail = max_chars
        result = text[-tail:]
    elif max_chars < 40:
        head = max_chars
        result = text[:head]
    else:
        marker = "\n[… omitted …]\n"
        body = max(2, max_chars - len(marker))
        head = max(1, int(body * 0.6))
        tail = max(1, body - head)
        result = text[:head] + marker + text[-tail:]
    while len(result.encode("utf-8")) > max_bytes and len(result) > 1:
        if tail_only:
            tail = max(1, tail - 1)
            result = text[-tail:]
        elif marker:
            # Preserve both semantic ends; shrink the larger slice first.
            if head >= tail and head > 1:
                head -= 1
            elif tail > 1:
                tail -= 1
            elif head > 1:
                head -= 1
            else:
                break
            result = text[:head] + marker + text[-tail:]
        else:
            head = max(1, head - 1)
            result = text[:head]
    return result


def _truncate_content(content: Any, max_tokens: int, policy: str) -> Any:
    if max_tokens <= 0:
        return ""
    if isinstance(content, str):
        return _truncate_text(content, max_tokens, tail_only=policy == POLICY_TAIL)
    if isinstance(content, Mapping):
        mutable = thaw_json(content)
        # Data blocks are atomic.  A truncated/base64 JSON fragment is neither
        # a valid image nor useful text, so callers must keep or drop it whole.
        if str(mutable.get("type") or "") == "data":
            return mutable
        # Tool-result payloads keep their structural id/name fields while only
        # shortening the potentially huge output.
        for key in ("output", "content", "text"):
            value = mutable.get(key)
            if isinstance(value, str):
                overhead = estimate_context_tokens({**mutable, key: ""})
                mutable[key] = _truncate_text(
                    value,
                    max(1, max_tokens - overhead),
                    tail_only=policy == POLICY_TAIL,
                )
                return mutable
            if key == "output" and isinstance(value, (list, Mapping)):
                if isinstance(value, list):
                    parts = []
                    for block in value:
                        if isinstance(block, Mapping) and block.get("text") is not None:
                            parts.append(str(block["text"]))
                        elif isinstance(block, str):
                            parts.append(block)
                        else:
                            parts.append(canonical_json(block))
                    flattened = "\n".join(parts)
                else:
                    flattened = canonical_json(value)
                overhead = estimate_context_tokens({**mutable, key: ""})
                mutable[key] = _truncate_text(
                    flattened,
                    max(1, max_tokens - overhead),
                    tail_only=policy == POLICY_TAIL,
                )
                return mutable
        return _truncate_text(canonical_json(mutable), max_tokens)
    if isinstance(content, (list, tuple)) and any(
        isinstance(block, Mapping) and str(block.get("type") or "") == "data"
        for block in content
    ):
        return thaw_json(content)
    return _truncate_text(canonical_json(content), max_tokens)


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
        content_ref: Optional[str] = None,
        content_hash: Optional[str] = None,
        token_estimate: Optional[int] = None,
        render_name: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "ContextItem":
        normalized_content = _normalize_context_content(content)
        if (
            isinstance(normalized_content, Mapping)
            and str(normalized_content.get("type") or "") == "data"
        ):
            truncation_policy = POLICY_DROP
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
            content=normalized_content,
            metadata=metadata or {},
        )

    def with_content(self, content: Any) -> "ContextItem":
        normalized_content = _normalize_context_content(content)
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
    truncation_policy: str = POLICY_HEAD_TAIL,
    render_role: str = "user",
    cache_class: str = "dynamic",
) -> ContextItem:
    """Create a framework-neutral, explicitly-provenanced text item."""
    return ContextItem.create(
        item_id=item_id,
        kind=kind,
        origin=origin,
        trust=trust,
        visibility=VISIBILITY_MODEL,
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


def _canonical_key(item: ContextItem) -> tuple[Any, ...]:
    phase = 0 if item.kind in _SYSTEM_KINDS else 1
    if item.kind in {KIND_REMINDER, KIND_STEER}:
        phase = 2
    return (
        phase,
        item.created_seq,
        _TRUST_RANK.get(item.trust, 99),
        -item.priority,
        item.item_id,
    )


class ContextAssembler:
    """Canonical ordering, explicit truncation and explainable selection."""

    def __init__(
        self,
        *,
        total_budget: int,
        budget_details: Optional[Mapping[str, int]] = None,
    ) -> None:
        self.total_budget = max(0, int(total_budget))
        self.budget_details = {
            str(key): max(0, int(value))
            for key, value in sorted((budget_details or {}).items())
        }

    @staticmethod
    def _mandatory(item: ContextItem) -> bool:
        if item.visibility == VISIBILITY_MANIFEST_ONLY:
            return True
        if item.kind in _TOOL_KINDS:
            return False
        return item.truncation_policy == POLICY_NEVER

    @staticmethod
    def _unit_key(items: Sequence[ContextItem]) -> tuple[Any, ...]:
        return (
            -max(item.priority for item in items),
            -max(item.created_seq for item in items),
            tuple(sorted(item.item_id for item in items)),
        )

    @staticmethod
    def _tokens(item: ContextItem) -> int:
        return 0 if item.visibility == VISIBILITY_MANIFEST_ONLY else item.token_estimate

    def _cap_item(
        self,
        item: ContextItem,
        records: dict[str, dict[str, Any]],
    ) -> Optional[ContextItem]:
        original = item.token_estimate
        if item.visibility == VISIBILITY_MANIFEST_ONLY or original <= item.token_budget:
            records[item.item_id] = {
                "action": "included",
                "original_tokens": original,
                "final_tokens": original,
            }
            return item
        if (
            item.truncation_policy in {POLICY_HEAD_TAIL, POLICY_TAIL}
            and item.token_budget > 0
        ):
            capped = item.with_content(
                _truncate_content(
                    item.content, item.token_budget, item.truncation_policy
                )
            )
            if capped.token_estimate > item.token_budget:
                records[item.item_id] = {
                    "action": "excluded",
                    "reason": "item_budget",
                    "original_tokens": original,
                    "final_tokens": 0,
                }
                return None
            records[item.item_id] = {
                "action": "truncated",
                "original_tokens": original,
                "final_tokens": capped.token_estimate,
            }
            return capped
        if item.truncation_policy == POLICY_NEVER:
            records[item.item_id] = {
                "action": "included",
                "original_tokens": original,
                "final_tokens": original,
            }
            return item
        records[item.item_id] = {
            "action": "excluded",
            "reason": "item_budget",
            "original_tokens": original,
            "final_tokens": 0,
        }
        return None

    def assemble(self, items: Iterable[ContextItem]) -> ContextAssembly:
        raw_items = tuple(items)
        item_ids = [item.item_id for item in raw_items]
        duplicates = sorted(
            item_id for item_id in set(item_ids) if item_ids.count(item_id) > 1
        )
        if duplicates:
            raise ValueError(f"context item_id values must be unique: {duplicates}")
        original_items = sorted(raw_items, key=_canonical_key)
        records: dict[str, dict[str, Any]] = {}
        capped: list[ContextItem] = []
        excluded: list[ContextItem] = []
        for item in original_items:
            candidate = self._cap_item(item, records)
            if candidate is None:
                excluded.append(item)
            else:
                capped.append(candidate)

        pair_groups: dict[str, list[ContextItem]] = {}
        singles: list[list[ContextItem]] = []
        for item in capped:
            if item.kind in _TOOL_KINDS:
                pair_groups.setdefault(item.pair_id, []).append(item)
            else:
                singles.append([item])

        units = list(singles)
        for pair_id, pair_items in sorted(pair_groups.items()):
            calls = [item for item in pair_items if item.kind == KIND_TOOL_CALL]
            results = [item for item in pair_items if item.kind == KIND_TOOL_RESULT]
            # A pair id may describe either one call/result or one provider
            # parallel-call batch.  A batch is valid only when every call has
            # exactly one result; the whole unit is then selected atomically.
            if not pair_id or not calls or len(calls) != len(results):
                for item in pair_items:
                    records[item.item_id].update(
                        action="excluded", reason="malformed_tool_pair", final_tokens=0
                    )
                    excluded.append(item)
                continue
            units.append(pair_items)

        mandatory = [
            unit for unit in units if any(self._mandatory(item) for item in unit)
        ]
        optional = [unit for unit in units if unit not in mandatory]
        mandatory.sort(key=lambda unit: _canonical_key(min(unit, key=_canonical_key)))
        optional.sort(key=self._unit_key)

        included: list[ContextItem] = []
        used = 0
        over_budget = False

        for unit in mandatory:
            included.extend(unit)
            used += sum(self._tokens(item) for item in unit)
            over_budget = over_budget or used > self.total_budget

        for unit in optional:
            unit_tokens = sum(self._tokens(item) for item in unit)
            remaining = max(0, self.total_budget - used)
            is_pair = any(item.kind in _TOOL_KINDS for item in unit)
            if unit_tokens <= remaining:
                included.extend(unit)
                used += unit_tokens
                continue

            if is_pair:
                calls = [item for item in unit if item.kind == KIND_TOOL_CALL]
                results = [item for item in unit if item.kind == KIND_TOOL_RESULT]
                call_tokens = sum(self._tokens(item) for item in calls)
                result_budget = remaining - call_tokens
                if result_budget > 0 and len(results) == 1 and len(calls) == 1:
                    result = results[0]
                    if result.truncation_policy in {POLICY_HEAD_TAIL, POLICY_TAIL}:
                        shrunk = result.with_content(
                            _truncate_content(
                                result.content,
                                result_budget,
                                result.truncation_policy,
                            )
                        )
                        if call_tokens + self._tokens(shrunk) <= remaining:
                            records[result.item_id].update(
                                action="truncated",
                                final_tokens=shrunk.token_estimate,
                            )
                            included.extend([*calls, shrunk])
                            used += call_tokens + self._tokens(shrunk)
                            continue
                for item in unit:
                    records[item.item_id].update(
                        action="excluded", reason="paired_budget", final_tokens=0
                    )
                    excluded.append(item)
                continue

            item = unit[0]
            if remaining > 0 and item.truncation_policy in {
                POLICY_HEAD_TAIL,
                POLICY_TAIL,
            }:
                shrunk = item.with_content(
                    _truncate_content(item.content, remaining, item.truncation_policy)
                )
                if (
                    self._tokens(shrunk) < self._tokens(item)
                    and self._tokens(shrunk) <= remaining
                ):
                    records[item.item_id].update(
                        action="truncated", final_tokens=shrunk.token_estimate
                    )
                    included.append(shrunk)
                    used += self._tokens(shrunk)
                    continue
            records[item.item_id].update(
                action="excluded", reason="budget", final_tokens=0
            )
            excluded.append(item)

        included.sort(key=_canonical_key)
        excluded_by_id = {item.item_id: item for item in excluded}
        excluded = sorted(excluded_by_id.values(), key=_canonical_key)

        included_manifest = []
        excluded_manifest = []
        included_ids = {item.item_id for item in included}
        final_by_id = {item.item_id: item for item in included}
        for original in original_items:
            record = dict(records.get(original.item_id) or {})
            final = final_by_id.get(original.item_id, original)
            entry = final.to_manifest()
            entry.update(record)
            if original.item_id in included_ids:
                included_manifest.append(entry)
            else:
                excluded_manifest.append(entry)

        payload = {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "total_budget": self.total_budget,
            "used_tokens": used,
            "over_budget": over_budget,
            "included": included_manifest,
            "excluded": excluded_manifest,
        }
        if self.budget_details:
            payload["budget_details"] = dict(self.budget_details)
        manifest_hash = stable_hash(payload)
        return ContextAssembly(
            included=tuple(included),
            excluded=tuple(excluded),
            _manifest=payload,
            manifest_hash=manifest_hash,
            used_tokens=used,
            total_budget=self.total_budget,
            over_budget=over_budget,
        )


__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "CONTEXT_SEQUENCE_STRIDE",
    "SESSION_CONTEXT_META_KEY",
    "ContextAssembler",
    "ContextAdapterProtocol",
    "ContextAssembly",
    "ContextItem",
    "KIND_ASSISTANT",
    "KIND_ATTACHMENT",
    "KIND_COMPACTION",
    "KIND_MEMORY",
    "KIND_IDENTITY",
    "KIND_PROJECT",
    "KIND_REFERENCE",
    "KIND_REMINDER",
    "KIND_STEER",
    "KIND_SYSTEM_RULE",
    "KIND_TOOL_CALL",
    "KIND_TOOL_RESULT",
    "KIND_USER_INPUT",
    "POLICY_DROP",
    "POLICY_HEAD_TAIL",
    "POLICY_NEVER",
    "POLICY_TAIL",
    "VISIBILITY_MANIFEST_ONLY",
    "VISIBILITY_MODEL",
    "estimate_context_tokens",
    "make_text_context_item",
    "session_context_metadata",
]
