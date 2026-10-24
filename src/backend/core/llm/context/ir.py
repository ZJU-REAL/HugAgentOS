"""Assemble context items into the window sent to the model."""
from __future__ import annotations
import logging
from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional, Sequence
from core.harness.execution_manifest import stable_hash
from core.llm.context import content
from core.llm.context import ir_constants
from core.llm.context.items import ContextAssembly, ContextItem

logger = logging.getLogger(__name__)

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
        if item.visibility == ir_constants.VISIBILITY_MANIFEST_ONLY:
            return True
        if item.kind in ir_constants.STEP_KINDS:
            return False
        return item.truncation_policy == ir_constants.POLICY_NEVER

    @staticmethod
    def _unit_key(items: Sequence[ContextItem]) -> tuple[Any, ...]:
        return (
            -max(item.priority for item in items),
            -max(item.created_seq for item in items),
            tuple(sorted(item.item_id for item in items)),
        )

    @staticmethod
    def _tokens(item: ContextItem) -> int:
        return 0 if item.visibility == ir_constants.VISIBILITY_MANIFEST_ONLY else item.token_estimate

    def _cap_item(
        self,
        item: ContextItem,
        records: dict[str, dict[str, Any]],
    ) -> Optional[ContextItem]:
        original = item.token_estimate
        # Media still contributes to whole-context occupancy, but cannot spend
        # or be cut by the per-item text allowance.
        budget = item.token_budget + sum(
            content.estimate_context_tokens(block) for block in content.tool_result_images(item.content)
        )
        if (
            item.visibility == ir_constants.VISIBILITY_MANIFEST_ONLY
            or item.truncation_policy == ir_constants.POLICY_NEVER
            or original <= budget
        ):
            records[item.item_id] = {
                "action": "included",
                "original_tokens": original,
                "final_tokens": original,
            }
            return item
        if item.truncation_policy in {ir_constants.POLICY_HEAD_TAIL, ir_constants.POLICY_TAIL} and item.token_budget > 0:
            target = budget
            capped = item.with_content(
                content.truncate_content(item.content, target, item.truncation_policy)
            )
            # JSON escaping of newlines/quotes can exceed the initial text
            # allowance. Refit using the measured cost, never discard an image
            # result just because its accompanying text needs a smaller slice.
            while capped.token_estimate > budget and target > 1:
                target = max(1, target - (capped.token_estimate - budget))
                capped = item.with_content(
                    content.truncate_content(item.content, target, item.truncation_policy)
                )
            if capped.token_estimate <= budget:
                records[item.item_id] = {
                    "action": "truncated",
                    "original_tokens": original,
                    "final_tokens": capped.token_estimate,
                }
                return capped
        if item.kind == ir_constants.KIND_TOOL_RESULT:
            # Dropping a result leaves its call unanswered, which the model reads
            # as a tool that returned nothing. Keep the pair and say what happened.
            pruned = item.with_content(content.prune_tool_output(item.content, original))
            logger.warning(
                "[context] tool result %s cannot fit its %d-token budget (%d tokens); "
                "sent as a placeholder",
                item.item_id,
                budget,
                original,
            )
            records[item.item_id] = {
                "action": "pruned",
                "reason": "item_budget",
                "original_tokens": original,
                "final_tokens": pruned.token_estimate,
            }
            return pruned
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
            if item.kind in ir_constants.TOOL_KINDS:
                if not item.pair_id:
                    return False
                pairs.setdefault(item.pair_id, []).append(item)
        for pair_items in pairs.values():
            calls = [item for item in pair_items if item.kind == ir_constants.KIND_TOOL_CALL]
            results = [item for item in pair_items if item.kind == ir_constants.KIND_TOOL_RESULT]
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
            if item.kind == ir_constants.KIND_TOOL_CALL and item.pair_id:
                # One pair id may name a provider parallel batch (several calls
                # in one unit); the same id across two units is unresolvable.
                if call_units.get(item.pair_id, item.unit_id) != item.unit_id:
                    ambiguous_pairs.add(item.pair_id)
                call_units[item.pair_id] = item.unit_id
        units: dict[str, list[ContextItem]] = {}
        for candidate in capped:
            if candidate.kind in ir_constants.TOOL_KINDS and candidate.pair_id in ambiguous_pairs:
                records[candidate.item_id].update(
                    action="excluded", reason="malformed_tool_pair", final_tokens=0
                )
                excluded.append(candidate)
                continue
            if candidate.kind == ir_constants.KIND_TOOL_RESULT and candidate.pair_id in call_units:
                candidate = replace(candidate, unit_id=call_units[candidate.pair_id])
            units.setdefault(candidate.unit_id, []).append(candidate)

        # The request that opened the turn in progress. Everything after it is
        # the model's own live work and is protected as a whole.
        request_index = -1
        for item in original_items:
            if (
                item.kind == ir_constants.KIND_USER_INPUT
                and item.truncation_policy == ir_constants.POLICY_NEVER
                and item.visibility == ir_constants.VISIBILITY_MODEL
            ):
                request_index = arrival[item.item_id]

        def unit_start(unit: Sequence[ContextItem]) -> int:
            return min(arrival[item.item_id] for item in unit)

        mandatory: list[list[ContextItem]] = []
        protected: list[list[ContextItem]] = []
        aside: list[list[ContextItem]] = []
        conversation: list[list[ContextItem]] = []
        for unit in units.values():
            is_step = any(item.kind in ir_constants.STEP_KINDS for item in unit)
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
            elif all(item.kind in ir_constants.CONVERSATION_KINDS for item in unit):
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
                and item.truncation_policy in {ir_constants.POLICY_HEAD_TAIL, ir_constants.POLICY_TAIL}
            ):
                shrunk = item.with_content(
                    content.truncate_content(item.content, remaining, item.truncation_policy)
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
                    if item.kind != ir_constants.KIND_TOOL_RESULT or item.truncation_policy == ir_constants.POLICY_NEVER:
                        continue
                    pruned = item.with_content(content.prune_tool_output(item.content, item.token_estimate))
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
            "schema_version": ir_constants.CONTEXT_SCHEMA_VERSION,
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
