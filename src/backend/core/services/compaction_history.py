"""Convert persisted chat rows into model-facing compaction history."""

from __future__ import annotations

from typing import Any, Dict, List

from core.chat import inflight
from core.chat.context import build_effective_user_message
from core.llm import compaction as C
from core.llm.context_ir import SESSION_CONTEXT_META_KEY
from core.llm.model_steps import assistant_row_replay
from core.services.chat_reference_service import render_reference_block


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
            # The model-facing sequence comes from the step record; rows that
            # predate it fall back to the streamed display order, and rows
            # older than that are replayed as an explicit digest. A cancelled
            # turn is marked so the next turn does not mistake a cut-off answer
            # for a finished one.
            out.extend(
                assistant_row_replay(
                    content=content or "",
                    model_steps=getattr(msg, "model_steps", None),
                    thinking=getattr(msg, "thinking", None),
                    tool_calls=getattr(msg, "tool_calls", None),
                    segments=extra.get("segments"),
                    cancelled=bool(extra.get("cancelled")),
                )
            )
        else:
            row = {"role": role or "user", "content": content}
            if isinstance(extra.get(SESSION_CONTEXT_META_KEY), dict):
                row[SESSION_CONTEXT_META_KEY] = dict(extra[SESSION_CONTEXT_META_KEY])
            out.append(row)
    return out
