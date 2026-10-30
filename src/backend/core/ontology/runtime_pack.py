"""No workflow ever activates, so nothing is added to prompts or tool tags."""

from __future__ import annotations

from typing import Any


def register_runtime_asset_tags(_runtime: dict[str, Any], **_kwargs: Any) -> None:
    return None


def activate_runtime_for_asset(_runtime: dict[str, Any], **_kwargs: Any) -> list[dict[str, Any]]:
    return []


def render_runtime_prompt(_runtime: dict[str, Any]) -> str:
    return ""
