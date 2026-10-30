"""Answers are never held for a post-answer review."""

from __future__ import annotations

from typing import Any


def requires_output_review(_runtime: dict[str, Any]) -> bool:
    return False


def claim_output_review(_runtime: dict[str, Any], *, owner: str) -> bool:
    return False


def complete_output_review(_runtime: dict[str, Any], *, owner: str, **_kwargs: Any) -> None:
    return None


def release_output_review(_runtime: dict[str, Any], *, owner: str) -> None:
    return None
