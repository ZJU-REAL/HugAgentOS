"""Task response projection."""
def approval_view(row):
    """The pending approval with what the approval card must show before the user decides: where the
    action goes, which chat it changes and all of its text. The stored approval keeps id, action and at.

    ``detail``: target ("cloud", "device:<id>" or None for an action without one), place (the same in
    words), chat_title (the chat a session action changes, None when it has none or is not the
    owner's to see), lines (every field of the action as "label：value") and body (the whole text
    that would go out, "" when there is none)."""
    pending = (row.checkpoint or {}).get("pending_approval")
    if not pending: return pending
    from sqlalchemy.orm import object_session
    from personal_agent.channels.im_actions import _place, explain
    from personal_agent.models import PersonalAgent
    action, db = pending.get("action") or {}, object_session(row)
    agent = db.get(PersonalAgent, row.agent_id) if db is not None else None
    if agent is None: return pending
    lines, body = explain(db, agent, action)
    place, chat = _place(db, agent, action) if action.get("kind") == "hugagent.session" else (None, None)
    return {**pending, "detail": {"target": action.get("target"), "place": place, "chat_title": chat, "lines": lines, "body": body}}

def task_view(row):
    from personal_agent.scheduling import proactive_tasks
    return {"id": row.id, "chat_id": row.chat_id, "goal": row.goal, "status": "queued" if row.status == "computer_wait" else row.status, "run_id": row.run_id,
            "waiting_reason": row.waiting_reason, "result": row.result,
            "pending_approval": approval_view(row),
            # A proactive check started this task, whoever continues it: each of its actions is approved
            # by itself (no "allow for this task"), and the card says that the user did not ask for it.
            "proactive": proactive_tasks.began(row),
            "uncertain_actions": [key for key, value in (row.checkpoint or {}).get("action_receipts", {}).items() if row.status == "waiting" and (value.get("status") == "uncertain" or value.get("outcome_unknown"))],
            "title": row.label_text, "progress": ((row.checkpoint or {}).get("progress") or {}).get("summary"),
            "question": ((row.checkpoint or {}).get("pending_question") or {}).get("text") if row.status == "waiting" else None,
            "outcome": (row.checkpoint or {}).get("outcome"),
            "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}
