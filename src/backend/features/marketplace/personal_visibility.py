"""Community marketplace items are public when enabled."""


def get_hidden_item_ids(db, kind: str, user_id) -> set[str]:
    return set()


def is_item_visible(db, kind: str, item_id: str, user_id) -> bool:
    return True


__all__ = ["get_hidden_item_ids", "is_item_visible"]
