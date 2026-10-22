"""Channel sessions responsibilities."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from integrations.channels.protocol import InboundMsg
from core.chat.context import resolve_enabled_capabilities
from core.db.models import ChatSession
from integrations.channels.models import ChannelConnection
from core.chat.service import ChatService
from integrations.channels import inbound_state

def conv_key(msg: "InboundMsg") -> str:
    return f"{msg.channel_id}:{msg.external_conversation_id}"


def already_handled(message_id: str) -> bool:
    if not message_id:
        return False
    if message_id in inbound_state.seen_message_ids:
        return True
    inbound_state.seen_message_ids[message_id] = None
    if len(inbound_state.seen_message_ids) > inbound_state.SEEN_MAX:
        inbound_state.seen_message_ids.popitem(last=False)
    return False


def is_reset_command(text: Optional[str]) -> bool:
    return (text or "").strip().lower() in inbound_state.RESET_COMMANDS


def reset_session(db, conn: ChannelConnection, msg: InboundMsg) -> None:
    """Soft-delete the current channel session. The next non-command message creates a fresh empty session via _find_or_create_session."""
    existing = (
        db.query(ChatSession)
        .filter(
            ChatSession.channel_id == conn.channel_id,
            ChatSession.external_conversation_id == msg.external_conversation_id,
            ChatSession.deleted_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        ChatService(db).delete_session_force(
            existing.chat_id, actor_user_id=conn.owner_user_id
        )


def find_or_create_session(
    db, conn: ChannelConnection, msg: InboundMsg
) -> ChatSession:
    """Reuse or create a session keyed by (channel_id, external_conversation_id), owned by the owner."""
    existing = (
        db.query(ChatSession)
        .filter(
            ChatSession.channel_id == conn.channel_id,
            ChatSession.external_conversation_id == msg.external_conversation_id,
            # Exclude soft-deleted sessions: the channel-side /new clear works by soft-deleting
            # the current session — without this filter the next message would hit the old
            # session again, making the clear a no-op.
            ChatSession.deleted_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        # Backfill the p2p peer id: proactive delivery (automation etc.) needs it to locate
        # the recipient when sending back in a private chat; old sessions predate this field,
        # so we backfill on every inbound message.
        if msg.chat_type == "p2p" and msg.sender_id:
            meta = dict(existing.extra_data or {})
            if meta.get("channel_peer_id") != msg.sender_id:
                meta["channel_peer_id"] = msg.sender_id
                existing.extra_data = meta
                db.commit()
        return existing

    chat_service = ChatService(db)
    title = (msg.text or "渠道会话").strip()[:40] or "渠道会话"
    extra: Dict[str, Any] = {
        "source": f"channel:{conn.channel_type}",
        "channel_chat_type": msg.chat_type,
    }
    if msg.chat_type == "p2p" and msg.sender_id:
        extra["channel_peer_id"] = msg.sender_id
    session = chat_service.create_session(
        user_id=conn.owner_user_id,
        title=title,
        extra_data=extra,
    )
    session.channel_id = conn.channel_id
    session.external_conversation_id = msg.external_conversation_id
    db.commit()
    db.refresh(session)
    return session


def load_history(db, chat_id: str, owner_id: str) -> List[Dict[str, Any]]:
    """Load history as session_messages — same source as the web path (checkpoint-aware + keeps tool calls/results).

    Goes through ``compaction_service.load_session_history`` (the same entry point as the web
    pipeline), not the old "``list_all_messages`` + keep only user/assistant plain text". The
    old approach **dropped wholesale** the assistant turns' ``tool_calls`` and tool results, as
    well as empty-text pure-tool turns — so in multi-turn channel sessions the model couldn't
    see which tools it called last turn or what results it got. With thinking disabled it would
    very easily redo the same work from scratch, degenerating into "I'll get right on generating…"
    style idling until hitting max_iters. With the same-source loader, the model gets real tool
    context across turns and reuses compaction checkpoints (large sessions no longer replay in full).

    When there is no access permission (theoretically impossible — the session was just created
    by _find_or_create_session and belongs to the owner), load_session_history returns None;
    we fall back to empty history here.
    """
    from core.chat.compaction.service import load_session_history

    chat_service = ChatService(db)
    return load_session_history(chat_service, chat_id, owner_id) or []


def resolve_enabled(
    db, conn: ChannelConnection, owner_id: str
) -> Dict[str, Optional[List[str]]]:
    """Resolve the owner's capabilities, then narrow them by the resource_scope whitelist."""
    skills, agents, mcps = resolve_enabled_capabilities(db, owner_id)
    scope = conn.resource_scope if isinstance(conn.resource_scope, dict) else {}
    scoped_skills = scope.get("skill_ids")
    scoped_kbs = scope.get("kb_ids")
    return {
        # Whitelist present → narrow to it (the owner must still own these; catalog gating applies as usual)
        "enabled_skills": scoped_skills if isinstance(scoped_skills, list) else skills,
        "enabled_agents": agents,
        "enabled_mcps": mcps,
        "enabled_kbs": scoped_kbs if isinstance(scoped_kbs, list) else None,
    }
