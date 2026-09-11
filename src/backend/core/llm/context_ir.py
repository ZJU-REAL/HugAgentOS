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

CONTEXT_SCHEMA_VERSION = "harness.context.v2"
SESSION_CONTEXT_META_KEY = "_context_item"
CONTEXT_SEQUENCE_STRIDE = 1_000

KIND_SYSTEM_RULE = "system_rule"
KIND_USER_INPUT = "user_input"
KIND_ASSISTANT = "assistant_history"
KIND_MEMORY = "memory"
KIND_IDENTITY = "identity"
KIND_PROJECT = "project_material"
KIND_THINKING = "thinking"
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

_TOOL_KINDS = {KIND_TOOL_CALL, KIND_TOOL_RESULT}
# One ReAct step is the atomic unit of history: what the model said (thinking,
# text, tool calls) together with the results those calls produced. These
# kinds are never budgeted block by block.
STEP_KINDS = frozenset({KIND_ASSISTANT, KIND_THINKING, KIND_TOOL_CALL, KIND_TOOL_RESULT})
# Conversation history is selected as a contiguous tail; everything else
# (memory, project material, reminders, attachments, the compaction summary)
# keeps priority-based selection because it is not ordered by time.
CONVERSATION_KINDS = STEP_KINDS | {KIND_USER_INPUT}
_TRUNCATABLE_BLOCK_TYPES = {"tool_result"}
_PRUNED_OUTPUT_TEMPLATE = "[tool output pruned to fit the context budget: {tokens} tokens omitted]"


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
    """Shorten prunable text only; every structured block keeps its shape.

    Plain text and a tool result's output are the only things a budget may
    shorten. Any other block — thinking, tool call, data, or a shape this
    module does not know — is returned untouched so the caller keeps or drops
    it whole. Rewriting such a block into a JSON string would hand the model
    its own reasoning back as an answer.
    """
    if max_tokens <= 0:
        return ""
    if isinstance(content, str):
        return _truncate_text(content, max_tokens, tail_only=policy == POLICY_TAIL)
    if isinstance(content, Mapping):
        mutable = thaw_json(content)
        block_type = str(mutable.get("type") or "")
        if block_type not in _TRUNCATABLE_BLOCK_TYPES:
            return mutable
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
        return mutable
    return thaw_json(content)


def _prune_tool_output(content: Any, original_tokens: int) -> Any:
    """Replace a tool result's output with an explicit placeholder."""
    mutable = thaw_json(content)
    if not isinstance(mutable, dict) or str(mutable.get("type") or "") != "tool_result":
        raise ValueError("only tool_result content can be pruned")
    for key in ("output", "content", "text"):
        if key in mutable:
            mutable[key] = _PRUNED_OUTPUT_TEMPLATE.format(tokens=original_tokens)
            return mutable
    mutable["output"] = _PRUNED_OUTPUT_TEMPLATE.format(tokens=original_tokens)
    return mutable


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
            unit_id=str(unit_id or f"item:{item_id}"),
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


class ContextAssembler:
    """Explicit, unit-level selection in context order.

    The unit of selection is a whole ReAct step — the reasoning, text and tool
    calls the model produced together with the results those calls returned —
    never a single block. A step is kept or dropped as one; the only thing a
    budget may shorten inside it is a tool result's output.

    Ordering is **not** the assembler's job: items arrive in the order the agent
    context already holds them, and they leave in that same order. The live
    user instruction carries a sequence 64 strides above the context tail (see
    ``next_request_sequence``), so sorting by sequence would move it behind the
    turn it had just started.

    Budget pressure is resolved in a fixed order: per-item caps first, then
    tool outputs of older steps are pruned (oldest first), and only then are
    whole steps cut from the oldest end of the conversation. The steps of the
    turn in progress — everything after the current user instruction — are
    protected: they are never pruned, cut or split. When they alone exceed the
    budget the assembly reports ``over_budget`` instead of quietly removing
    anything; compaction owns that case.
    """

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
        if item.kind in STEP_KINDS:
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
        if (
            item.visibility == VISIBILITY_MANIFEST_ONLY
            or item.truncation_policy == POLICY_NEVER
            or original <= item.token_budget
        ):
            records[item.item_id] = {
                "action": "included",
                "original_tokens": original,
                "final_tokens": original,
            }
            return item
        if item.truncation_policy in {POLICY_HEAD_TAIL, POLICY_TAIL} and item.token_budget > 0:
            capped = item.with_content(
                _truncate_content(item.content, item.token_budget, item.truncation_policy)
            )
            if capped.token_estimate <= item.token_budget:
                records[item.item_id] = {
                    "action": "truncated",
                    "original_tokens": original,
                    "final_tokens": capped.token_estimate,
                }
                return capped
        records[item.item_id] = {
            "action": "excluded",
            "reason": "item_budget",
            "original_tokens": original,
            "final_tokens": 0,
        }
        return None

    @staticmethod
    def _tool_pairs_balanced(unit: Sequence[ContextItem]) -> bool:
        pairs: dict[str, list[ContextItem]] = {}
        for item in unit:
            if item.kind in _TOOL_KINDS:
                if not item.pair_id:
                    return False
                pairs.setdefault(item.pair_id, []).append(item)
        for pair_items in pairs.values():
            calls = [item for item in pair_items if item.kind == KIND_TOOL_CALL]
            results = [item for item in pair_items if item.kind == KIND_TOOL_RESULT]
            if not calls or len(calls) != len(results):
                return False
        return True

    def assemble(self, items: Iterable[ContextItem]) -> ContextAssembly:
        raw_items = tuple(items)
        item_ids = [item.item_id for item in raw_items]
        duplicates = sorted(
            item_id for item_id in set(item_ids) if item_ids.count(item_id) > 1
        )
        if duplicates:
            raise ValueError(f"context item_id values must be unique: {duplicates}")
        original_items = list(raw_items)
        arrival = {item.item_id: index for index, item in enumerate(original_items)}
        records: dict[str, dict[str, Any]] = {}
        excluded: list[ContextItem] = []

        capped: list[ContextItem] = []
        for item in original_items:
            candidate = self._cap_item(item, records)
            if candidate is None:
                excluded.append(item)
                continue
            capped.append(candidate)

        # A tool result belongs to the step that issued its call, whatever unit
        # the caller stamped on it: the pair id is the structural link.
        call_units: dict[str, str] = {}
        ambiguous_pairs: set[str] = set()
        for item in capped:
            if item.kind == KIND_TOOL_CALL and item.pair_id:
                # One pair id may name a provider parallel batch (several calls
                # in one unit); the same id across two units is unresolvable.
                if call_units.get(item.pair_id, item.unit_id) != item.unit_id:
                    ambiguous_pairs.add(item.pair_id)
                call_units[item.pair_id] = item.unit_id
        units: dict[str, list[ContextItem]] = {}
        for candidate in capped:
            if candidate.kind in _TOOL_KINDS and candidate.pair_id in ambiguous_pairs:
                records[candidate.item_id].update(
                    action="excluded", reason="malformed_tool_pair", final_tokens=0
                )
                excluded.append(candidate)
                continue
            if candidate.kind == KIND_TOOL_RESULT and candidate.pair_id in call_units:
                candidate = replace(candidate, unit_id=call_units[candidate.pair_id])
            units.setdefault(candidate.unit_id, []).append(candidate)

        # The request that opened the turn in progress. Everything after it is
        # the model's own live work and is protected as a whole.
        request_index = -1
        for item in original_items:
            if (
                item.kind == KIND_USER_INPUT
                and item.truncation_policy == POLICY_NEVER
                and item.visibility == VISIBILITY_MODEL
            ):
                request_index = arrival[item.item_id]

        def unit_start(unit: Sequence[ContextItem]) -> int:
            return min(arrival[item.item_id] for item in unit)

        mandatory: list[list[ContextItem]] = []
        protected: list[list[ContextItem]] = []
        aside: list[list[ContextItem]] = []
        conversation: list[list[ContextItem]] = []
        for unit in units.values():
            is_step = any(item.kind in STEP_KINDS for item in unit)
            if any(self._mandatory(item) for item in unit):
                mandatory.append(unit)
            elif is_step and request_index >= 0 and unit_start(unit) > request_index:
                protected.append(unit)
            elif not self._tool_pairs_balanced(unit):
                for item in unit:
                    records[item.item_id].update(
                        action="excluded", reason="malformed_tool_pair", final_tokens=0
                    )
                    excluded.append(item)
            elif all(item.kind in CONVERSATION_KINDS for item in unit):
                conversation.append(unit)
            else:
                aside.append(unit)

        mandatory.sort(key=unit_start)
        protected.sort(key=unit_start)
        conversation.sort(key=unit_start)
        # Selection order only for non-conversation material; it never reaches
        # the output, which is re-sorted into context order.
        aside.sort(key=self._unit_key)

        def unit_tokens(unit: Sequence[ContextItem]) -> int:
            return sum(self._tokens(item) for item in unit)

        included: list[ContextItem] = []
        used = 0
        for unit in mandatory + protected:
            included.extend(unit)
            used += unit_tokens(unit)
        over_budget = used > self.total_budget

        for unit in aside:
            tokens = unit_tokens(unit)
            remaining = max(0, self.total_budget - used)
            if tokens <= remaining:
                included.extend(unit)
                used += tokens
                continue
            item = unit[0]
            if (
                len(unit) == 1
                and remaining > 0
                and item.truncation_policy in {POLICY_HEAD_TAIL, POLICY_TAIL}
            ):
                shrunk = item.with_content(
                    _truncate_content(item.content, remaining, item.truncation_policy)
                )
                if self._tokens(shrunk) < self._tokens(item) and self._tokens(shrunk) <= remaining:
                    records[item.item_id].update(
                        action="truncated", final_tokens=shrunk.token_estimate
                    )
                    included.append(shrunk)
                    used += self._tokens(shrunk)
                    continue
            for item in unit:
                records[item.item_id].update(action="excluded", reason="budget", final_tokens=0)
                excluded.append(item)

        # Conversation history: prune old tool outputs first, then cut whole
        # steps from the oldest end. Never a hole in the middle.
        pruned_items = 0
        conversation_tokens = sum(unit_tokens(unit) for unit in conversation)
        if used + conversation_tokens > self.total_budget:
            for unit in conversation:
                if used + conversation_tokens <= self.total_budget:
                    break
                for index, item in enumerate(unit):
                    if used + conversation_tokens <= self.total_budget:
                        break
                    if item.kind != KIND_TOOL_RESULT or item.truncation_policy == POLICY_NEVER:
                        continue
                    pruned = item.with_content(_prune_tool_output(item.content, item.token_estimate))
                    if self._tokens(pruned) >= self._tokens(item):
                        continue
                    conversation_tokens -= self._tokens(item) - self._tokens(pruned)
                    unit[index] = pruned
                    records[item.item_id].update(action="pruned", final_tokens=pruned.token_estimate)
                    pruned_items += 1
        cut_units = 0
        while conversation and used + conversation_tokens > self.total_budget:
            dropped = conversation.pop(0)
            conversation_tokens -= unit_tokens(dropped)
            cut_units += 1
            for item in dropped:
                records[item.item_id].update(action="excluded", reason="budget_cut", final_tokens=0)
                excluded.append(item)
        for unit in conversation:
            included.extend(unit)
            used += unit_tokens(unit)

        included.sort(key=lambda item: arrival[item.item_id])
        excluded_by_id = {item.item_id: item for item in excluded}
        excluded = sorted(excluded_by_id.values(), key=lambda item: arrival[item.item_id])

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
            "protected_units": len(protected),
            "cut_units": cut_units,
            "pruned_items": pruned_items,
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
            cut_units=cut_units,
            pruned_items=pruned_items,
        )


__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "CONTEXT_SEQUENCE_STRIDE",
    "CONVERSATION_KINDS",
    "STEP_KINDS",
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
