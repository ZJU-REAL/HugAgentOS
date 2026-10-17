"""Task response projection."""
def task_view(row):
    return {"id": row.id, "chat_id": row.chat_id, "goal": row.goal, "status": "queued" if row.status == "computer_wait" else row.status, "run_id": row.run_id,
            "waiting_reason": row.waiting_reason, "result": row.result,
            "pending_approval": (row.checkpoint or {}).get("pending_approval"),
            "uncertain_actions": [key for key, value in (row.checkpoint or {}).get("action_receipts", {}).items() if row.status == "waiting" and (value.get("status") == "uncertain" or value.get("outcome_unknown"))],
            "title": row.label_text, "progress": ((row.checkpoint or {}).get("progress") or {}).get("summary"),
            "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}
