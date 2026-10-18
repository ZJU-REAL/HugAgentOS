"""Provider-neutral object directory results."""
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class StorageEntry:
    name: str
    key: str
    is_directory: bool = False
    size: int = 0
    modified_at: datetime | None = None
    etag: str | None = None
    version: str | None = None

@dataclass(frozen=True)
class DirectoryPage:
    entries: tuple[StorageEntry, ...]
    next_cursor: str | None = None

def validate_key(key: str, *, directory: bool = False) -> str:
    if not isinstance(key, str) or "\\" in key or any(ord(c) < 32 for c in key):
        raise ValueError("Invalid storage path")
    value = key.removesuffix("/") if directory else key
    if value and any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("Storage paths must be canonical and relative")
    if not value and not directory:
        raise ValueError("An object path is required")
    return value + "/" if directory and value else value

def page_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("Directory page size must be between 1 and 1000")
    return limit
