"""Which pages the agent actually uses: a front read or a search hit marks a page used.

Only the working agent counts (its turns and background tasks); Dreaming reads every page
and would keep everything alive. Marking is best effort: a failure never fails the tool.
"""
import logging

from core.db import engine
from core.infra.time import utc_now
from personal_agent.memory import layout
from personal_agent.memory.models import PersonalMemoryUsage

log = logging.getLogger(__name__)


def touch(ident, paths):
    """Mark each page in paths (workspace-relative or /workspace/...) used now."""
    from personal_agent.workspace.files import relative
    rels = sorted({rel for rel in (relative(p) for p in paths or ()) if layout.is_page(rel)})
    if not rels: return
    try:
        with engine.SessionLocal() as db:
            mark(db, ident, rels)
            db.commit()
    except Exception as exc:   # usage is a hint; the read or search already succeeded
        log.warning("memory usage not recorded (%s)", type(exc).__name__)


def mark(db, ident, rels, now=None):
    now = now or utc_now()
    for rel in rels:
        row = db.get(PersonalMemoryUsage, (ident, rel))
        if row is None:
            db.add(PersonalMemoryUsage(agent_id=ident, path=rel, first_seen_at=now, last_used_at=now, uses=1))
        else:
            row.last_used_at, row.uses = now, (row.uses or 0) + 1
    db.flush()


def rows(db, ident):
    return {row.path: row for row in db.query(PersonalMemoryUsage).filter(PersonalMemoryUsage.agent_id == ident)}


def move(db, ident, old, new):
    """A page merged or renamed into another: the survivor keeps the more recent use."""
    gone = db.get(PersonalMemoryUsage, (ident, old))
    if gone is None: return
    kept = db.get(PersonalMemoryUsage, (ident, new))
    if kept is None:
        db.add(PersonalMemoryUsage(agent_id=ident, path=new, first_seen_at=gone.first_seen_at,
                                   last_used_at=gone.last_used_at, uses=gone.uses))
    else:
        if gone.last_used_at and (kept.last_used_at is None or gone.last_used_at > kept.last_used_at):
            kept.last_used_at = gone.last_used_at
        kept.uses = (kept.uses or 0) + (gone.uses or 0)
    db.delete(gone)
    db.flush()
