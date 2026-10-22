"""blocks responsibilities."""
from __future__ import annotations
from typing import Any, Mapping

def block_payload(block: Any) -> Any:
    if isinstance(block, Mapping):
        return dict(block)
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json")
    return block


def block_type(block: Any) -> str:
    if isinstance(block, Mapping):
        return str(block.get("type") or "")
    return str(getattr(block, "type", "") or "")


def block_text(block: Any) -> str:
    if isinstance(block, Mapping):
        return str(block.get("text") or "")
    return str(getattr(block, "text", "") or "")
