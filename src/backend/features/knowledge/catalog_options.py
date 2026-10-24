"""Knowledge options for the administrative agent capability picker."""
from features.knowledge.models import KBSpace


def list_options(db):
    spaces = db.query(KBSpace).filter(KBSpace.deleted_at.is_(None)).order_by(KBSpace.created_at.desc()).all()
    return [{"id": space.kb_id, "name": space.name, "description": space.description or ""} for space in spaces]
