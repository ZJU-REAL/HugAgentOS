"""Community knowledge-base grant policy: resources are owner-only."""

from __future__ import annotations

from sqlalchemy.orm import Session


def initial_visibility_grants(db: Session, user_id: str, visibility: str) -> list[tuple[str, str]]:
    return []


__all__ = ["initial_visibility_grants"]
