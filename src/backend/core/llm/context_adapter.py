"""The sole Canonical Context IR ↔ AgentScope message adapter.

Compatibility roles are a rendering detail here.  Provenance and trust travel
in structured message metadata and never have to be inferred from XML labels.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional, Sequence

from agentscope.message import Msg, TextBlock

from core.immutable import thaw_json
from core.llm.execution_manifest import stable_hash
from core.llm.context_ir import (
    CONTEXT_SEQUENCE_STRIDE,
    ContextItem,
    KIND_ASSISTANT,
    KIND_ATTACHMENT,
    KIND_COMPACTION,
    KIND_MEMORY,
    KIND_PROJECT,
    KIND_REFERENCE,
    KIND_REMINDER,
    KIND_SYSTEM_RULE,
    KIND_TOOL_CALL,
    KIND_TOOL_RESULT,
    KIND_USER_INPUT,
    POLICY_DROP,
    POLICY_HEAD_TAIL,
    POLICY_NEVER,
    SESSION_CONTEXT_META_KEY,
    VISIBILITY_MANIFEST_ONLY,
    VISIBILITY_MODEL,
    estimate_context_tokens,
    make_text_context_item,
)

CONTEXT_META_KEY = "harness_context_items"
PROVIDER_CONTEXT_META_KEY = "_harness_context_item"


def _block_payload(block: Any) -> Any:
    if isinstance(block, Mapping):
        return dict(block)
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json")
    return block


def _block_type(block: Any) -> str:
    if isinstance(block, Mapping):
        return str(block.get("type") or "")
    return str(getattr(block, "type", "") or "")


def _block_text(block: Any) -> str:
    if isinstance(block, Mapping):
        return str(block.get("text") or "")
    return str(getattr(block, "text", "") or "")


class AgentScopeContextAdapter:
    """Translate the IR while preserving current model-facing roles/blocks."""

    @staticmethod
    def wrap_content(content: Any) -> list[Any]:
        """Construct AgentScope-compatible blocks inside the sole SDK seam."""
        if isinstance(content, str):
            return [TextBlock(type="text", text=content)] if content else []
        if isinstance(content, list):
            return content
        return [content] if content else []

    @staticmethod
    def _defaults(role: str, block_type: str) -> dict[str, Any]:
        if block_type == "tool_call":
            return {
                "kind": KIND_TOOL_CALL,
                "origin": "agent:tool_call",
                "trust": "assistant",
                "priority": 700,
                "token_budget": 4_000,
                "truncation_policy": POLICY_NEVER,
                "render_role": "assistant",
            }
        if block_type == "tool_result":
            return {
                "kind": KIND_TOOL_RESULT,
                "origin": "tool:result",
                "trust": "tool",
                "priority": 700,
                "token_budget": 20_000,
                "truncation_policy": POLICY_HEAD_TAIL,
                "render_role": "assistant",
            }
        if block_type == "data":
            return {
                "kind": KIND_ATTACHMENT,
                "origin": "user:attachment",
                "trust": "user",
                "priority": 650,
                "token_budget": 4_000,
                "truncation_policy": POLICY_DROP,
                "render_role": "user",
            }
        if role == "system":
            return {
                "kind": KIND_SYSTEM_RULE,
                "origin": "platform:system_prompt",
                "trust": "platform",
                "priority": 1_000,
                "token_budget": 100_000,
                "truncation_policy": POLICY_NEVER,
                "render_role": "system",
            }
        if role == "assistant":
            return {
                "kind": KIND_ASSISTANT,
                "origin": "assistant:history",
                "trust": "assistant",
                "priority": 400,
                "token_budget": 20_000,
                "truncation_policy": POLICY_HEAD_TAIL,
                "render_role": "assistant",
            }
        return {
            "kind": KIND_USER_INPUT,
            "origin": "user:chat",
            "trust": "user",
            "priority": 500,
            "token_budget": 100_000,
            "truncation_policy": POLICY_HEAD_TAIL,
            "render_role": "user",
        }

    @staticmethod
    def _metadata_entries(message: Msg) -> list[Mapping[str, Any]]:
        metadata = getattr(message, "metadata", None) or {}
        raw = metadata.get(CONTEXT_META_KEY) if isinstance(metadata, Mapping) else None
        if not isinstance(raw, list):
            return []
        return [entry for entry in raw if isinstance(entry, Mapping)]

    def items_from_messages(
        self,
        messages: Sequence[Msg],
        *,
        summary_text: Any = None,
        promote_latest_user: bool = True,
    ) -> list[ContextItem]:
        items: list[ContextItem] = []
        summary = str(summary_text or "")
        tool_occurrences: dict[str, int] = {}
        pending_tool_pairs: dict[str, list[str]] = {}
        orphan_results: dict[str, int] = {}
        for message_index, message in enumerate(messages):
            role = str(getattr(message, "role", "user") or "user")
            name = str(getattr(message, "name", role) or role)
            blocks = list(getattr(message, "content", None) or [])
            entries = self._metadata_entries(message)
            for block_index, block in enumerate(blocks):
                block_type = _block_type(block)
                payload = _block_text(block) if block_type == "text" else _block_payload(block)
                defaults = self._defaults(role, block_type)
                explicit = entries[block_index] if block_index < len(entries) else {}
                if (
                    not explicit
                    and summary
                    and role == "user"
                    and isinstance(payload, str)
                    and payload == summary
                ):
                    defaults.update(
                        kind=KIND_COMPACTION,
                        origin="harness:compaction",
                        trust="system",
                        priority=850,
                        truncation_policy=POLICY_HEAD_TAIL,
                        render_role="user",
                    )
                raw_pair_id = str(
                    payload.get("id")
                    if block_type in {"tool_call", "tool_result"} and isinstance(payload, Mapping)
                    else ""
                )
                pair_id = str(explicit.get("pair_id") or "")
                if block_type == "tool_call" and raw_pair_id:
                    occurrence = tool_occurrences.get(raw_pair_id, 0) + 1
                    tool_occurrences[raw_pair_id] = occurrence
                    pair_id = f"{raw_pair_id}#{occurrence}"
                    pending_tool_pairs.setdefault(raw_pair_id, []).append(pair_id)
                elif block_type == "tool_result" and raw_pair_id:
                    pending = pending_tool_pairs.get(raw_pair_id) or []
                    if pending:
                        pair_id = pending.pop(0)
                    else:
                        occurrence = orphan_results.get(raw_pair_id, 0) + 1
                        orphan_results[raw_pair_id] = occurrence
                        pair_id = f"{raw_pair_id}#orphan-result-{occurrence}"
                item_id = str(
                    (
                        f"message:{message_index}:block:{block_index}:{block_type}:{pair_id}"
                        if block_type in {"tool_call", "tool_result"}
                        else explicit.get("item_id")
                        or f"message:{message_index}:block:{block_index}"
                    )
                )
                created_seq = int(
                    explicit.get("created_seq")
                    if explicit.get("created_seq") is not None
                    else message_index * CONTEXT_SEQUENCE_STRIDE + block_index
                )
                item = ContextItem.create(
                    item_id=item_id,
                    kind=str(explicit.get("kind") or defaults["kind"]),
                    origin=str(explicit.get("origin") or defaults["origin"]),
                    trust=str(explicit.get("trust") or defaults["trust"]),
                    visibility=str(explicit.get("visibility") or VISIBILITY_MODEL),
                    priority=int(
                        explicit["priority"]
                        if explicit.get("priority") is not None
                        else defaults["priority"]
                    ),
                    token_budget=int(
                        explicit["token_budget"]
                        if explicit.get("token_budget") is not None
                        else defaults["token_budget"]
                    ),
                    truncation_policy=str(
                        explicit.get("truncation_policy") or defaults["truncation_policy"]
                    ),
                    content=payload,
                    cache_class=str(explicit.get("cache_class") or "dynamic"),
                    created_seq=created_seq,
                    render_role=str(explicit.get("render_role") or defaults["render_role"]),
                    # The outer message carries the actual transport name.
                    # Persisted manifests contain only its hash.
                    render_name=name,
                    pair_id=pair_id,
                    message_group=str(explicit.get("message_group") or f"message:{message_index}"),
                    content_ref=(
                        str(explicit["content_ref"]) if explicit.get("content_ref") else None
                    ),
                    content_hash=(
                        str(explicit["content_hash"]) if explicit.get("content_hash") else None
                    ),
                    token_estimate=(
                        int(explicit["token_estimate"])
                        if explicit.get("token_estimate") is not None
                        else None
                    ),
                    metadata={},
                )
                items.append(item)

        # The latest real user instruction is a hard inclusion. Memory and
        # reminders may render as user for compatibility but keep distinct kind.
        user_indices = [i for i, item in enumerate(items) if item.kind == KIND_USER_INPUT]
        if promote_latest_user and user_indices:
            idx = user_indices[-1]
            items[idx] = replace(
                items[idx],
                priority=max(900, items[idx].priority),
                truncation_policy=POLICY_NEVER,
            )
        return items

    def items_from_session_dict(
        self,
        message: Mapping[str, Any],
        *,
        created_seq: int,
    ) -> list[ContextItem]:
        role = str(message.get("role") or "user")
        role = {"human": "user", "ai": "assistant", "tool": "assistant"}.get(role, role)
        content = message.get("content")
        blocks = (
            content
            if isinstance(content, list)
            else ([{"type": "text", "text": content}] if content else [])
        )
        explicit = message.get(SESSION_CONTEXT_META_KEY)
        metadata_entries = []
        if isinstance(explicit, Mapping):
            metadata_entries = [dict(explicit) for _ in blocks]
        msg = Msg(
            name=str(message.get("name") or role),
            role=role if role in {"system", "assistant", "user"} else "user",
            content=blocks,
            metadata={CONTEXT_META_KEY: metadata_entries} if metadata_entries else {},
        )
        # A persisted row is converted in isolation, so it cannot know whether
        # it is the current user instruction. Promotion happens once, later,
        # when ManifestBoundAgent sees the complete final request.
        items = self.items_from_messages([msg], promote_latest_user=False)
        has_explicit_seq = isinstance(explicit, Mapping) and explicit.get("created_seq") is not None
        return [
            replace(
                item,
                item_id=(
                    item.item_id
                    if isinstance(explicit, Mapping) and explicit.get("item_id")
                    else f"session:{created_seq}:block:{index}"
                ),
                created_seq=(
                    item.created_seq
                    if has_explicit_seq
                    else created_seq * CONTEXT_SEQUENCE_STRIDE + index
                ),
                message_group=f"session:{created_seq}",
            )
            for index, item in enumerate(items)
        ]

    def message_from_session_dict(
        self,
        message: Mapping[str, Any],
        *,
        created_seq: int,
    ) -> Msg:
        """Render one persisted history record through the canonical adapter."""
        items = self.items_from_session_dict(message, created_seq=created_seq)
        rendered = self.messages_from_items(items)
        if rendered:
            return rendered[0]
        role = str(message.get("role") or "user")
        role = {"human": "user", "ai": "assistant", "tool": "assistant"}.get(role, role)
        if role not in {"system", "assistant", "user"}:
            role = "user"
        return Msg(
            name=str(message.get("name") or role),
            role=role,
            content=[],
            metadata={CONTEXT_META_KEY: []},
        )

    def messages_from_items(self, items: Iterable[ContextItem]) -> list[Msg]:
        visible = [item for item in items if item.visibility == VISIBILITY_MODEL]
        messages: list[Msg] = []
        current_group: Optional[tuple[str, str]] = None
        current_items: list[ContextItem] = []

        def flush() -> None:
            nonlocal current_group, current_items
            if not current_items or current_group is None:
                return
            blocks = []
            manifests = []
            for item in current_items:
                content = thaw_json(item.content)
                if isinstance(content, str):
                    blocks.append(TextBlock(type="text", text=content))
                    manifests.append(item.to_manifest())
                elif isinstance(content, Mapping):
                    blocks.append(dict(content))
                    manifests.append(item.to_manifest())
                elif isinstance(content, list):
                    for block_index, block in enumerate(content):
                        blocks.append(block)
                        normalized = (
                            _block_text(block)
                            if _block_type(block) == "text"
                            else _block_payload(block)
                        )
                        derived = replace(
                            item,
                            item_id=f"{item.item_id}:block:{block_index}",
                            truncation_policy=(
                                POLICY_DROP
                                if _block_type(block) == "data"
                                else item.truncation_policy
                            ),
                        ).with_content(normalized)
                        manifests.append(derived.to_manifest())
                elif content is not None:
                    blocks.append(TextBlock(type="text", text=str(content)))
                    manifests.append(item.with_content(str(content)).to_manifest())
            if blocks:
                name = str(current_items[0].render_name or current_group[1])
                messages.append(
                    Msg(
                        name=name,
                        role=current_group[1],
                        content=blocks,
                        metadata={CONTEXT_META_KEY: manifests},
                    )
                )
            current_group = None
            current_items = []

        for item in visible:
            group = (item.message_group, item.render_role)
            if current_group is not None and group != current_group:
                flush()
            current_group = group
            current_items.append(item)
        flush()
        return messages

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
                )
            )
        return items

    @staticmethod
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

    def reference_items_from_execution_manifest(self, manifest: Any) -> list[ContextItem]:
        if hasattr(manifest, "to_dict"):
            payload = manifest.to_dict()
        elif isinstance(manifest, Mapping):
            payload = dict(manifest)
        else:
            return []
        sections = (payload.get("prompt_manifest") or {}).get("sections") or []
        items: list[ContextItem] = []
        for index, section in enumerate(sections):
            if not isinstance(section, Mapping):
                continue
            section_id = str(section.get("id") or "")
            content_hash = str(section.get("content_hash") or "")
            if not section_id or not content_hash:
                continue
            is_project = section_id == "runtime/project"
            live_content = ""
            content_reader = getattr(manifest, "prompt_section_content", None)
            if callable(content_reader):
                live_content = str(content_reader(index) or "")
            if is_project and live_content:
                items.append(
                    ContextItem.create(
                        item_id=f"prompt:{index}:{section_id}",
                        kind=KIND_PROJECT,
                        origin=str(section.get("origin") or "workspace:project"),
                        trust=str(section.get("trust") or "workspace"),
                        visibility=VISIBILITY_MODEL,
                        priority=int(section.get("priority") or 900),
                        token_budget=max(
                            1,
                            int(section.get("budget") or section.get("token_estimate") or 1),
                        ),
                        truncation_policy=POLICY_HEAD_TAIL,
                        content=live_content,
                        content_ref=(
                            str(section["reference"]) if section.get("reference") else None
                        ),
                        content_hash=content_hash,
                        token_estimate=int(section.get("token_estimate") or 0),
                        cache_class=str(section.get("cache_class") or "workspace"),
                        created_seq=1,
                        render_role="system",
                        render_name="system",
                        message_group=f"prompt:{index}",
                    )
                )
                continue
            items.append(
                ContextItem.create(
                    item_id=f"prompt:{index}:{section_id}",
                    kind=KIND_PROJECT if is_project else KIND_SYSTEM_RULE,
                    origin=str(section.get("origin") or "prompt:unknown"),
                    trust=str(section.get("trust") or "platform"),
                    visibility=VISIBILITY_MANIFEST_ONLY,
                    priority=int(section.get("priority") or 0),
                    token_budget=int(section.get("budget") or 0),
                    truncation_policy=POLICY_NEVER,
                    content=None,
                    content_ref=str(
                        section.get("reference")
                        or f"prompt:{section_id}@{section.get('version') or '1'}"
                    ),
                    content_hash=content_hash,
                    token_estimate=int(section.get("token_estimate") or 0),
                    cache_class=str(section.get("cache_class") or "stable"),
                    created_seq=-10_000 + index,
                    render_role="system",
                    message_group=f"prompt:{index}",
                )
            )
        return items


def append_state_context_item(state: Any, item: ContextItem) -> Msg:
    """Render and append one explicitly-provenanced item to AgentScope state."""
    messages = AgentScopeContextAdapter().messages_from_items([item])
    if len(messages) != 1:
        raise ValueError("context item did not render to exactly one message")
    message = messages[0]
    state.context.append(message)
    return message


def render_text_block(text: str) -> TextBlock:
    """Construct an AgentScope text block inside the SDK adapter boundary."""
    return TextBlock(type="text", text=str(text or ""))


def next_context_sequence(messages: Sequence[Any]) -> int:
    """Allocate one monotonic sequence across replay and live injections."""
    max_sequence = -CONTEXT_SEQUENCE_STRIDE
    for message_index, message in enumerate(messages):
        entries = AgentScopeContextAdapter._metadata_entries(message)
        blocks = list(getattr(message, "content", None) or [])
        for block_index, _block in enumerate(blocks or [None]):
            explicit = entries[block_index] if block_index < len(entries) else {}
            candidate = explicit.get("created_seq")
            if candidate is None:
                candidate = message_index * CONTEXT_SEQUENCE_STRIDE + block_index
            try:
                max_sequence = max(max_sequence, int(candidate))
            except (TypeError, ValueError):
                continue
    return ((max_sequence // CONTEXT_SEQUENCE_STRIDE) + 1) * CONTEXT_SEQUENCE_STRIDE


def next_request_sequence(messages: Sequence[Any]) -> int:
    """Reserve a small pre-reply lane, then place the user instruction last.

    AgentScope runs ``on_reply`` middleware before it appends ``inputs`` to
    state. File/project attachment middleware can therefore add a few explicit
    context items first without colliding with, or sorting after, the request.
    """
    pre_reply_slots = 64
    return next_context_sequence(messages) + pre_reply_slots * CONTEXT_SEQUENCE_STRIDE


def render_context_item(item: ContextItem) -> Msg:
    """Render one IR item for ``Agent.reply`` without mutating agent state."""
    messages = AgentScopeContextAdapter().messages_from_items([item])
    if len(messages) != 1:
        raise ValueError("context item did not render to exactly one message")
    return messages[0]


def render_session_input(message: Mapping[str, Any], *, created_seq: int) -> Msg:
    """Render one pending positional row at an explicit live request sequence."""
    adapter = AgentScopeContextAdapter()
    items = adapter.items_from_session_dict(message, created_seq=0)
    positioned = [
        replace(
            item,
            created_seq=created_seq + index,
            message_group=f"request:{created_seq}",
        )
        for index, item in enumerate(items)
    ]
    rendered = adapter.messages_from_items(positioned)
    if len(rendered) != 1:
        raise ValueError("session input did not render to exactly one message")
    return rendered[0]


def append_context_item(agent: Any, item: ContextItem) -> Msg:
    """Append one explicitly-provenanced item through the adapter seam."""
    return append_state_context_item(agent.state, item)


def append_context_text(
    agent: Any,
    text: str,
    *,
    kind: str,
    origin: str,
    trust: str,
    priority: int = 700,
    token_budget: int = 4_000,
    truncation_policy: str = POLICY_HEAD_TAIL,
    render_role: str = "user",
) -> Msg:
    """Append provenanced text without creating AgentScope messages elsewhere."""
    created_seq = next_context_sequence(agent.state.context)
    item = make_text_context_item(
        text,
        item_id=f"{origin}:{created_seq}",
        kind=kind,
        origin=origin,
        trust=trust,
        created_seq=created_seq,
        priority=priority,
        token_budget=token_budget,
        truncation_policy=truncation_policy,
        render_role=render_role,
    )
    return append_context_item(agent, item)


__all__ = [
    "AgentScopeContextAdapter",
    "CONTEXT_META_KEY",
    "PROVIDER_CONTEXT_META_KEY",
    "SESSION_CONTEXT_META_KEY",
    "append_context_item",
    "append_state_context_item",
    "append_context_text",
    "make_text_context_item",
    "next_context_sequence",
    "next_request_sequence",
    "render_context_item",
    "render_session_input",
    "render_text_block",
]
