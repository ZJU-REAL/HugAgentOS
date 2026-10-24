"""Reconstruct active command events at the persisted-history boundary."""
from core.db.models import ChatRun
from core.db.models.command import CommandTask
from core.sandbox.commands.delivery import notice_text


def completion_context(row):
    """One stable sideband record per completed command, never a chat row."""
    from core.llm.context.ir_constants import KIND_REMINDER, POLICY_NEVER, SESSION_CONTEXT_META_KEY
    from core.llm.context.items import make_text_context_item, session_context_metadata
    event_id = "command_event_" + row["task_id"]
    prompt = notice_text([row])
    item = make_text_context_item(
        prompt, item_id=event_id, kind=KIND_REMINDER,
        origin="harness:command_completion", trust="system", created_seq=0,
        priority=950, token_budget=4000, truncation_policy=POLICY_NEVER,
    )
    return {"message_id": event_id, "role": "user", "content": prompt,
            SESSION_CONTEXT_META_KEY: session_context_metadata(item),
            "extra_data": {"hidden_in_chat": True, "system_wake": "command_finish"}}


def append_active_completions(db, chat_id, history):
    """Preserve delivered sideband facts when steering rebuilds a live snapshot."""
    rows = (db.query(CommandTask).join(ChatRun, CommandTask.notice_run_id == ChatRun.run_id)
            .filter(CommandTask.chat_id == chat_id,
                    CommandTask.notice_status == "delivered",
                    CommandTask.delivery_mode == "boundary",
                    ChatRun.chat_id == chat_id,
                    ChatRun.user_id == CommandTask.user_id,
                    ChatRun.status.in_(("pending", "running")),
                    ChatRun.writer_slot == "main")
            .order_by(CommandTask.task_id).all())
    existing = {message.get("message_id") for message in history}
    from core.sandbox.commands.store import view
    events = [completion_context(view(row)) for row in rows
              if "command_event_" + row.task_id not in existing]
    return [*history, *events]
