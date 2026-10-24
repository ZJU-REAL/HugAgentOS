"""Convert persisted chat rows into model-facing compaction history."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from core.chat import inflight
from core.chat.context import build_effective_user_message
from core.llm.context import compaction as C
from core.llm.context.ir_constants import SESSION_CONTEXT_META_KEY
from core.llm.runtime.model_steps import assistant_row_replay, record_assistant_step
from core.chat.references import render_reference_block


def _cancelled_steps(steps: Any, content: str) -> Any:
    """Keep the displayed unfinished answer after the last completed step.

    Completed steps are authoritative for native reasoning and tool pairing.
    The display projection can additionally contain a text tail from the model
    call the user cancelled. Never promote that call's incomplete reasoning.
    """
    if not isinstance(steps, list) or not steps or not content:
        return steps
    step_texts = [
        "".join(
            str(block.get("text") or "")
            for block in step.get("blocks") or []
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
        for step in steps
        if step.get("kind") == "assistant"
    ]
    completed_text = "".join(step_texts)
    final_text = next((text for text in reversed(step_texts) if text), "")
    if content.startswith(completed_text):
        tail = content[len(completed_text) :]
    elif final_text and content.startswith(final_text):
        # content_replace resets the display but retains earlier model steps.
        tail = content[len(final_text) :]
    else:
        # A divergent older display record cannot be spliced into native steps.
        # Keep it explicitly labelled rather than guessing a continuation.
        tail = "\n[中断前已显示的回答，与完整步骤记录不一致]\n" + content
    if not tail:
        return steps
    return [
        *steps,
        record_assistant_step([{"type": "text", "text": tail}], provider="", model="", protocol=""),
    ]


def normalize_rows(rows: List[Any], live_run_ids: frozenset = frozenset()) -> List[Dict[str, Any]]:
    """Normalize message rows, excluding internal compaction checkpoints.

    A row still being written by a live run is skipped: it is half-written by
    definition and would show the model a truncated version of its own reply.
    """
    out: List[Dict[str, Any]] = []
    for msg in rows:
        role = getattr(msg, "role", None)
        extra = getattr(msg, "extra_data", None) or {}
        if role == "system" and extra.get("kind") == C.COMPACTION_CHECKPOINT_KIND:
            continue
        owner = inflight.marker(extra)
        if owner and owner["run_id"] in live_run_ids:
            continue
        if role == "assistant" and extra.get("model_history_display_only"):
            continue
        content = getattr(msg, "content", "")
        if role == "user":
            quoted = extra.get("quoted_follow_up")
            row = {
                "role": "user",
                "content": build_effective_user_message(
                    content,
                    quoted,
                    render_reference_block(extra.get("referenced_chats")),
                ),
            }
            if isinstance(extra.get(SESSION_CONTEXT_META_KEY), dict):
                row[SESSION_CONTEXT_META_KEY] = dict(extra[SESSION_CONTEXT_META_KEY])
            out.append(row)
        elif role == "assistant":
            out.extend(
                assistant_row_replay(
                    content=content or "",
                    model_steps=(
                        _cancelled_steps(getattr(msg, "model_steps", None), content or "")
                        if extra.get("cancelled")
                        else getattr(msg, "model_steps", None)
                    ),
                    cancelled=bool(extra.get("cancelled")),
                )
            )
        else:
            row = {"role": role or "user", "content": content}
            if isinstance(extra.get(SESSION_CONTEXT_META_KEY), dict):
                row[SESSION_CONTEXT_META_KEY] = dict(extra[SESSION_CONTEXT_META_KEY])
            out.append(row)
    return out
