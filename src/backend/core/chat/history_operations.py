"""User-scoped history operations for tool hosts."""

def history_operations(*, user_id, chat_id=None, project_id=None):
    def _list(query: str, limit: int):
        from core.db.engine import SessionLocal
        from core.chat.references import list_referencable_sessions, session_brief

        with SessionLocal() as db:
            sessions = list_referencable_sessions(
                db,
                user_id,
                project_id=project_id,
                query=query,
                limit=limit,
                exclude_chat_id=chat_id,
            )
            return {
                "ok": True,
                "scope": "project" if project_id else "user",
                "count": len(sessions),
                "chats": [session_brief(session) for session in sessions],
            }


    def _read(target_chat_id: str, mode: str, offset: int, limit: int):
        from core.db.engine import SessionLocal
        from core.chat.references import read_session_messages, session_card

        with SessionLocal() as db:
            card = session_card(db, target_chat_id, user_id)
            if card is None:
                return {"ok": False, "error": f"会话不存在或你无权访问：{target_chat_id}"}
            if mode == "card":
                return {"ok": True, "mode": "card", **card}
            page = read_session_messages(db, target_chat_id, user_id, offset=offset, limit=limit)
            if page is None:
                return {"ok": False, "error": f"会话不存在或你无权访问：{target_chat_id}"}
            return {"ok": True, "mode": "full", "title": card["title"], **page}


    async def _digest(target_chat_id: str):
        from core.chat.references import build_session_digest

        result = await build_session_digest(target_chat_id, user_id)
        if result is None:
            return {"ok": False, "error": f"会话不存在或你无权访问：{target_chat_id}"}
        return {"ok": True, "mode": "digest", **result}

    return _list, _read, _digest
