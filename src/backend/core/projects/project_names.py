"""Project folder names and local workspace slugs."""
from __future__ import annotations
from typing import List

def slugify(name: str) -> str:
    """Lowercase, ASCII-safe, hyphen-joined slug for the local sandbox mount name."""
    out = []
    for ch in (name or "").strip().lower():
        if ch.isalnum() and ord(ch) < 128:
            out.append(ch)
        elif ch in (" ", "-", "_", "."):
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "project"


def next_available_folder_name(base: str, existing: List) -> str:
    base = (base or "").strip() or "新项目"
    used = {row[0] for row in existing or []}
    if base not in used:
        return base
    index = 2
    while f"{base} ({index})" in used:
        index += 1
    return f"{base} ({index})"
