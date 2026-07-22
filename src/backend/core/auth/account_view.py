"""Community current-account response has no organization fields."""


def extend_current_account(db, user_id: str, data: dict) -> dict:
    return data


__all__ = ["extend_current_account"]
