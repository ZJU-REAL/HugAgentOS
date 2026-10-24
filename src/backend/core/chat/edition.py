"""Community chat presentation: owner-only fields and state."""


def extend_session_view(db, session, user_id: str, level: str, base: dict) -> dict:
    return base


def update_member_state(db, session, user_id: str, *, pinned=None, favorite=None) -> bool:
    return False


__all__ = ["extend_session_view", "update_member_state"]
