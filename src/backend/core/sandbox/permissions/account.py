"""Account-owned policy. API, visitor and module conversations have independent authority."""

from core.db.models import ChatSession, UserShadow
from core.db.reserved_chats import reserved_chat_owner
from core.auth.users import UserService
from core.harness.registry import client_sessions
from core.harness.tool_call_context import CURRENT_RUN_BINDING
from core.sandbox.permissions import approval
from core.sandbox.permissions.approval import normalize_approval_mode


def resolve_work_approval_mode(user_id: str | None, chat_id: str | None = None) -> str:
    """Preset for host work: a client-session lease wins over the account setting.

    With ``chat_id`` the lease of that chat applies; without it, the lease bound
    to the running tool's run. A revoked or expired lease raises ValueError
    rather than falling back to the account preset.
    """
    if user_id:
        binding = CURRENT_RUN_BINDING.get()
        run_id = binding[0] if binding and not chat_id else None
        leased = client_sessions.approval_mode(str(user_id), chat_id, run_id)
        if leased is not None:
            return normalize_approval_mode(leased)
    return approval.resolve_approval_mode(None, user_id=user_id)


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
            leased = client_sessions.approval_mode(user_id, chat_id)
            if leased is not None:
                return leased
        return normalize_approval_mode(
            UserService(db).get_user_settings(user_id).get("tool_approval_mode")
        )
