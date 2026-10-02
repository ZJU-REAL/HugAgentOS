"""Derived UI data only; model replay and tool expansion retain the original."""


def tool_calls_for_history(raw):
    from core.chat.display_bounds import bound_result_for_history

    if not isinstance(raw, list):
        return raw
    out = []
    for item in raw:
        if not isinstance(item, dict) or "result" not in item:
            out.append(item)
            continue
        bounded, clipped = bound_result_for_history(item["result"])
        out.append({**item, "result": bounded, "result_truncated": True} if clipped else item)
    return out


def display_default(context):
    # SQLAlchemy applies this to ORM AND Core/bulk writes. If a writer updates
    # unrelated fields, invalidate rather than risk a stale display projection.
    return tool_calls_for_history(context.get_current_parameters().get("tool_calls"))


def backfill_display(db, *, batch_size=10):
    """Resumable backfill in committed batches; use a dedicated session."""
    from sqlalchemy import update
    from core.db.models import ChatMessage

    total = 0
    after = ""
    while True:
        rows = (
            db.query(ChatMessage.message_id, ChatMessage.tool_calls)
            .filter(
                ChatMessage.message_id > after,
                ChatMessage.tool_calls_display.is_(None),
                ChatMessage.tool_calls.is_not(None),
            )
            .order_by(ChatMessage.message_id)
            .limit(batch_size)
            .all()
        )
        if not rows:
            return total
        for mid, raw in rows:
            if raw is not None:
                # Compare-and-set: a concurrent writer cannot publish an old preview.
                result = db.execute(
                    update(ChatMessage)
                    .where(
                        ChatMessage.message_id == mid,
                        ChatMessage.tool_calls_display.is_(None),
                        ChatMessage.tool_calls == raw,
                    )
                    .values(tool_calls_display=tool_calls_for_history(raw))
                )
                total += result.rowcount
        after = rows[-1].message_id
        db.commit()
