"""Account-owned policy. API, visitor and module conversations have independent authority."""

from core.db.models import ChatSession, UserShadow
from core.db.reserved_chats import reserved_chat_owner
from core.auth.users import UserService
from core.sandbox.permissions.approval import normalize_approval_mode


def read_account_mode(user_id: str, chat_id: str | None = None) -> str | None:
    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        if db.get(UserShadow, user_id) is None:
            return None
        if chat_id:
            chat = db.get(ChatSession, chat_id)
            if (
                chat is None
                or chat.user_id != user_id
                or chat.deleted_at is not None
                or reserved_chat_owner(chat_id)
                or any(
                    (chat.extra_data or {}).get(key)
                    for key in ("agent_api_scope", "project_api_scope")
                )
            ):
                return None
        return normalize_approval_mode(
            UserService(db).get_user_settings(user_id).get("tool_approval_mode")
        )
