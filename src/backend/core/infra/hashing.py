"""Content hashes that are stable across processes and dict orderings."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_json_hash(value: Any) -> str:
    """SHA-256 of ``value`` as canonical JSON (sorted keys, compact, ``str`` fallback)."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
