"""Route committed personal registry changes back into the listener's durable queue."""

from core.db.models import Artifact, UserFolder
from core.services.artifact_edition import is_personal_artifact

from .database import subscribe
from .events import Change


def subscriptions(registry):
    def notify(user):
        if not registry._closed and registry._loop and not registry._loop.is_closed():
            registry._loop.call_soon_threadsafe(
                registry._inbox.put, user + ":", Change("refresh", "")
            )

    return [
        subscribe(UserFolder, lambda row: row.user_id, notify),
        subscribe(Artifact, lambda row: row.user_id if is_personal_artifact(row) else None, notify),
    ]


def projection_owners():
    """Recover DB-only changes after a crash before notification publication."""
    from core.db.engine import SessionLocal
    from core.services.artifact_edition import personal_artifact_predicates

    with SessionLocal() as db:
        folders = (
            db.query(UserFolder.user_id).filter(UserFolder.deleted_at.is_(None)).distinct().all()
        )
        artifacts = (
            db.query(Artifact.user_id)
            .filter(*personal_artifact_predicates(Artifact))
            .distinct()
            .all()
        )
        return {owner for owner, in folders + artifacts if owner}
