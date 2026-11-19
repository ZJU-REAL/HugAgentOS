"""Model configuration revision, committed atomically with configuration writes."""

from __future__ import annotations

from sqlalchemy.orm import Session

from core.db import config_revision

REVISION_KEY = "model_config_revision"


def read_revision(db: Session) -> str:
    return config_revision.read_revision(db, REVISION_KEY)


def bump_revision(db: Session) -> None:
    # The row also serializes concurrent configuration commits; it never
    # publishes ahead of the provider/role changes in this same transaction.
    config_revision.bump_revision(db, REVISION_KEY, "model_config")
