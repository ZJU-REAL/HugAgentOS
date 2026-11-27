"""Total-storage quota for the personal space (My Space)."""
from core.auth.capabilities import enforce_quota
from core.files.spaces.tree import tree_entries

# Usage is measured by walking the space; the bound only guards against runaway listings.
USAGE_SCAN_LIMIT = 200_000


def tree_bytes(storage, prefix: str) -> int:
    """Total size of the files stored under ``prefix``."""
    entries = tree_entries(storage, prefix, limit=USAGE_SCAN_LIMIT)
    return sum(item.size or 0 for item in entries if not item.is_directory)


def personal_usage_bytes(storage, user_id: str) -> int:
    from core.files.spaces.access import personal_grant

    return tree_bytes(storage, personal_grant(user_id).prefix)


def ensure_space_quota(db, storage, grant, incoming: int, *, replacing: int = 0) -> None:
    """413 when writing ``incoming`` bytes into a personal space would exceed its owner's quota.

    ``replacing`` is the size of the file being overwritten, which stops counting once replaced.
    Team spaces are not limited here.
    """
    if grant.kind != "personal":
        return
    enforce_quota(
        db,
        grant.owner_id,
        "my_space_quota_mb",
        lambda: tree_bytes(storage, grant.prefix) - replacing,
        incoming,
        "我的空间",
    )
