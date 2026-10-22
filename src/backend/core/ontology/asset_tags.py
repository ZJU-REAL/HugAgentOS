"""asset tags responsibilities."""
from __future__ import annotations
from typing import Any

def normalize_ontology_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        return []
    return sorted({str(item).strip() for item in items if str(item).strip()})
