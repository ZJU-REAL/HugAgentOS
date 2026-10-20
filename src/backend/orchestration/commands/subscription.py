"""Authenticated snapshot-and-tail subscription for ordinary-chat wake events."""
import asyncio
import json
from fastapi import HTTPException
from core.auth.backend import get_current_user, security
from core.services.commands import store
from core.db.models import ChatRun
from core.db.models.command import CommandTask
from orchestration.commands.events import MAX_EVENTS, get_command_events

HEARTBEAT_SECONDS = 15


def require_edit(chat_id, user_id):
    with store.SessionLocal() as db:
        if not store.can_edit(db, chat_id, user_id):
            raise HTTPException(404, "Conversation not found")


async def authorize(request, chat_id, expected_user=None):
    with store.SessionLocal() as db:
        credentials = await security(request)
        user = await get_current_user(request, credentials=credentials, db=db)
    if user.api_key_id or user.public_chat_visitor:
        raise HTTPException(403, "Command events require an account session")
    if expected_user is not None and user.user_id != expected_user:
        raise HTTPException(401, "Account identity changed")
    await asyncio.to_thread(require_edit, chat_id, user.user_id)
    return user.user_id


def snapshot(chat_id, user_id):
    with store.SessionLocal() as db:
        rows = (db.query(ChatRun).join(CommandTask, CommandTask.notice_run_id == ChatRun.run_id)
                .filter(CommandTask.chat_id == chat_id, CommandTask.user_id == user_id,
                        CommandTask.delivery_mode == "followup",
                        CommandTask.notice_status.in_(("reserved", "delivered")),
                        ChatRun.chat_id == chat_id, ChatRun.user_id == user_id)
                .distinct().order_by(ChatRun.created_at.desc(), ChatRun.run_id.desc())
                .limit(MAX_EVENTS).all())
        return [run.run_id for run in rows if run.recovery_snapshot]


def frame(run_ids):
    return "data: " + json.dumps({"type": "command_runs", "run_ids": list(dict.fromkeys(run_ids))}) + "\n\n"


async def subscribe(request, chat_id, user_id):
    events = get_command_events()
    # Capture first: a concurrent publish is in the snapshot, the tail, or both.
    cursor = await events.tail(user_id, chat_id)
    runs = await asyncio.to_thread(snapshot, chat_id, user_id)
    try:
        await authorize(request, chat_id, user_id)
        yield frame(runs)
        while True:
            cursor, runs, resync = await events.wait(
                user_id, chat_id, cursor, timeout=HEARTBEAT_SECONDS)
            await authorize(request, chat_id, user_id)
            if resync:
                runs = await asyncio.to_thread(snapshot, chat_id, user_id)
                await authorize(request, chat_id, user_id)
            yield frame(runs) if runs else ": keepalive\n\n"
    except HTTPException:
        return
