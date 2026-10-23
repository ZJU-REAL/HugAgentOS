"""Carry context provenance through provider-formatted messages."""
from __future__ import annotations
from core.models.constants import PROVIDER_CONTEXT_META_KEY
from typing import Any, Iterable, Mapping, Sequence
from agentscope.message import Msg
from core.infra.immutable import thaw_json
from core.harness.execution_manifest import stable_hash
from core.llm.context.ir_constants import CONTEXT_SEQUENCE_STRIDE, KIND_ASSISTANT, KIND_ATTACHMENT, KIND_SYSTEM_RULE, KIND_TOOL_CALL, KIND_TOOL_RESULT, KIND_USER_INPUT, POLICY_NEVER, VISIBILITY_MODEL
from core.llm.context.items import ContextItem
from core.llm.context.content import estimate_context_tokens

def annotate_provider_messages(
    self,
    source_messages: Sequence[Msg],
    formatted_messages: Sequence[Mapping[str, Any]],
    row_counts: Sequence[int],
) -> list[dict[str, Any]]:
    """Carry canonical provenance across an SDK formatter boundary.

    AgentScope's OpenAI formatter intentionally drops arbitrary message
    metadata and may expand one tool-result message into a tool row plus a
    synthetic media row.  ``row_counts`` is obtained by formatting each
    source message in isolation after a provider has rejected media.  This
    lets the retry inherit the original IR provenance without guessing it
    from compatibility roles or XML-like text.
    """
    annotated = [dict(message) for message in formatted_messages]
    cursor = 0
    for source, count in zip(source_messages, row_counts):
        entries = self._metadata_entries(source)
        source_rows = annotated[cursor : cursor + max(0, int(count))]
        cursor += max(0, int(count))
        if not entries:
            continue

        def pick(kind: str) -> Mapping[str, Any]:
            match = next((entry for entry in entries if entry.get("kind") == kind), None)
            if match is not None:
                return match
            user = next(
                (entry for entry in entries if entry.get("kind") == KIND_USER_INPUT),
                None,
            )
            return user or entries[0]

        for row in source_rows:
            role = str(row.get("role") or "user")
            name = str(row.get("name") or role)
            if name == "system-reminder":
                base = pick(KIND_TOOL_RESULT)
                metadata = {
                    "kind": KIND_ATTACHMENT,
                    "origin": f"{base.get('origin') or 'tool:result'}:media",
                    "trust": str(base.get("trust") or "tool"),
                    "priority": int(base.get("priority") or 700),
                }
            elif role == "tool":
                base = pick(KIND_TOOL_RESULT)
                metadata = {
                    "kind": KIND_TOOL_RESULT,
                    "origin": str(base.get("origin") or "tool:result"),
                    "trust": str(base.get("trust") or "tool"),
                    "priority": int(base.get("priority") or 700),
                }
            elif role == "assistant" and row.get("tool_calls"):
                base = pick(KIND_TOOL_CALL)
                metadata = {
                    "kind": KIND_TOOL_CALL,
                    "origin": str(base.get("origin") or "agent:tool_call"),
                    "trust": str(base.get("trust") or "assistant"),
                    "priority": int(base.get("priority") or 700),
                }
            else:
                base = pick("")
                metadata = {
                    key: base[key]
                    for key in ("kind", "origin", "trust", "priority")
                    if base.get(key) is not None
                }
            row[PROVIDER_CONTEXT_META_KEY] = metadata
    if cursor != len(annotated):
        # A formatter shape change must fail visibly rather than silently
        # assigning later rows the wrong trust boundary.
        raise ValueError(
            "provider formatter row mapping mismatch: "
            f"mapped={cursor} formatted={len(annotated)}"
        )
    return annotated


def items_from_provider_messages(
    self,
    messages: Sequence[Mapping[str, Any]],
) -> list[ContextItem]:
    """Reify an already-formatted provider retry as exact canonical items.

    A provider may reject media after the normal AgentScope request has
    already been assembled.  The retry is therefore a new execution
    surface.  Each complete wire message is kept atomically so its hash
    covers role, name, tool identifiers and content together; transient
    provenance metadata is removed before hashing and before transport.
    """
    items: list[ContextItem] = []
    pending_tool_pairs: dict[str, list[str]] = {}
    orphan_results: dict[str, int] = {}
    for index, raw_message in enumerate(messages):
        message = dict(raw_message)
        explicit = message.pop(PROVIDER_CONTEXT_META_KEY, None)
        explicit = explicit if isinstance(explicit, Mapping) else {}
        role = str(message.get("role") or "user")
        name = str(message.get("name") or role)
        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
            pair_id = f"provider-tool-batch:{index}"
            message_hash = stable_hash(message)
            message_tokens = estimate_context_tokens(message)
            for call_index, call in enumerate(tool_calls):
                call = call if isinstance(call, Mapping) else {}
                raw_pair_id = str(call.get("id") or "")
                pending_tool_pairs.setdefault(raw_pair_id, []).append(pair_id)
                items.append(
                    ContextItem.create(
                        item_id=f"provider-retry:{index}:tool-call:{call_index}",
                        kind=str(explicit.get("kind") or KIND_TOOL_CALL),
                        origin=str(explicit.get("origin") or "assistant:provider_tool_call"),
                        trust=str(explicit.get("trust") or "assistant"),
                        visibility=VISIBILITY_MODEL,
                        priority=int(explicit.get("priority") or 900),
                        token_budget=message_tokens if call_index == 0 else 0,
                        truncation_policy=POLICY_NEVER,
                        content=message if call_index == 0 else None,
                        content_ref=f"provider-message:{index}",
                        content_hash=message_hash,
                        token_estimate=message_tokens if call_index == 0 else 0,
                        cache_class="dynamic",
                        created_seq=index * CONTEXT_SEQUENCE_STRIDE + call_index,
                        render_role=role,
                        render_name=name,
                        pair_id=pair_id,
                        message_group=f"provider-retry:{index}",
                        unit_id=pair_id,
                    )
                )
            continue
        if role == "tool":
            raw_pair_id = str(message.get("tool_call_id") or "")
            pending = pending_tool_pairs.get(raw_pair_id) or []
            if pending:
                pair_id = pending.pop(0)
            else:
                occurrence = orphan_results.get(raw_pair_id, 0) + 1
                orphan_results[raw_pair_id] = occurrence
                pair_id = f"{raw_pair_id}#orphan-result-{occurrence}"
            defaults = {
                "kind": KIND_TOOL_RESULT,
                "origin": "tool:provider_result",
                "trust": "tool",
                "priority": 900,
                "pair_id": pair_id,
            }
        elif role == "system":
            defaults = {
                "kind": KIND_SYSTEM_RULE,
                "origin": "platform:provider_request",
                "trust": "platform",
                "priority": 1_000,
            }
        elif role == "assistant":
            defaults = {
                "kind": KIND_ASSISTANT,
                "origin": "assistant:provider_history",
                "trust": "assistant",
                "priority": 600,
            }
        else:
            defaults = {
                "kind": KIND_USER_INPUT,
                "origin": "user:provider_request",
                "trust": "user",
                "priority": 900,
            }
        items.append(
            ContextItem.create(
                item_id=str(explicit.get("item_id") or f"provider-retry:{index}"),
                kind=str(explicit.get("kind") or defaults["kind"]),
                origin=str(explicit.get("origin") or defaults["origin"]),
                trust=str(explicit.get("trust") or defaults["trust"]),
                visibility=VISIBILITY_MODEL,
                priority=int(explicit.get("priority") or defaults["priority"]),
                # These rows have already survived the canonical budget
                # pass.  Media recovery only removes data or replaces it
                # with a smaller textual description, so the retry must
                # preserve every resulting row exactly.
                token_budget=max(1, estimate_context_tokens(message)),
                truncation_policy=POLICY_NEVER,
                content=message,
                cache_class="dynamic",
                created_seq=int(
                    explicit.get("created_seq")
                    if explicit.get("created_seq") is not None
                    else index * CONTEXT_SEQUENCE_STRIDE
                ),
                render_role=role,
                render_name=name,
                pair_id=str(defaults.get("pair_id") or ""),
                message_group=f"provider-retry:{index}",
                unit_id=str(defaults.get("pair_id") or f"provider-retry:{index}"),
            )
        )
    return items


def provider_messages_from_items(items: Iterable[ContextItem]) -> list[dict[str, Any]]:
    """Render exact provider rows while stripping harness-only metadata."""
    rendered: list[dict[str, Any]] = []
    by_group: dict[str, dict[str, Any]] = {}
    deferred_groups: set[str] = set()
    for item in items:
        if item.visibility != VISIBILITY_MODEL:
            continue
        value = thaw_json(item.content)
        if value is None:
            deferred_groups.add(item.message_group)
            continue
        if not isinstance(value, Mapping):
            raise ValueError("provider retry context item must contain one message mapping")
        row = dict(value)
        row.pop(PROVIDER_CONTEXT_META_KEY, None)
        previous = by_group.get(item.message_group)
        if previous is not None:
            if previous != row:
                raise ValueError("provider retry group contains conflicting wire messages")
            continue
        by_group[item.message_group] = row
        rendered.append(row)
    missing = deferred_groups - set(by_group)
    if missing:
        raise ValueError(f"provider retry groups lack a wire row: {sorted(missing)}")
    return rendered
