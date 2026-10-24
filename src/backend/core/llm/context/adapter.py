"""Translate between the context IR and AgentScope messages."""
from __future__ import annotations
from core.llm.context import provider_conversion
from core.llm.context import reference_conversion
from core.llm.context import message_defaults
from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional, Sequence
from agentscope.message import Msg, TextBlock
from core.infra.immutable import thaw_json
from core.llm.context.ir_constants import CONTEXT_SEQUENCE_STRIDE, KIND_COMPACTION, KIND_USER_INPUT, POLICY_DROP, POLICY_HEAD_TAIL, POLICY_NEVER, SESSION_CONTEXT_META_KEY, STEP_KINDS, VISIBILITY_MODEL
from core.llm.context.items import ContextItem


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


CONTEXT_META_KEY = "harness_context_items"


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
        return message_defaults.defaults(role, block_type)

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
        # One ReAct step = what the model produced in one response plus the
        # results of the tool calls in it. A step opens on the first thinking,
        # text or tool_call block after a tool result (or after any other
        # message) and its results join it through the pending call ids. This
        # is the same boundary the reasoning-echo formatter flushes on.
        step_unit: Optional[str] = None
        step_closed = False
        pair_units: dict[str, str] = {}
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
                kind = str(explicit.get("kind") or defaults["kind"])
                if kind in STEP_KINDS and block_type != "tool_result":
                    if step_unit is None or step_closed:
                        step_unit = f"step:{message_index}:{block_index}"
                        step_closed = False
                    unit_id = step_unit
                    if block_type == "tool_call":
                        pair_units[pair_id] = unit_id
                elif block_type == "tool_result":
                    unit_id = pair_units.pop(pair_id, f"orphan:{item_id}")
                    step_closed = True
                else:
                    unit_id = f"item:{item_id}"
                    step_unit = None
                    step_closed = False
                    pair_units.clear()
                created_seq = int(
                    explicit.get("created_seq")
                    if explicit.get("created_seq") is not None
                    else message_index * CONTEXT_SEQUENCE_STRIDE + block_index
                )
                item = ContextItem.create(
                    item_id=item_id,
                    kind=kind,
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
                    unit_id=unit_id,
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
                unit_id=f"session:{created_seq}:{item.unit_id}",
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
        return provider_conversion.annotate_provider_messages(self, source_messages, formatted_messages, row_counts)

    def items_from_provider_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> list[ContextItem]:
        return provider_conversion.items_from_provider_messages(self, messages)

    @staticmethod
    def provider_messages_from_items(items: Iterable[ContextItem]) -> list[dict[str, Any]]:
        return provider_conversion.provider_messages_from_items(items)

    def reference_items_from_execution_manifest(self, manifest: Any) -> list[ContextItem]:
        return reference_conversion.reference_items_from_execution_manifest(self, manifest)
