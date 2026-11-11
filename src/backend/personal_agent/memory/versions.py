"""The write log: record each change, list them, prune old ones. Revert lives in store."""
import hashlib
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import aliased, defer

from core.infra.time import utc_now
from personal_agent.memory.models import PersonalMemoryVersion

KEEP_DAYS = 30


def digest(text):
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


def record(db, agent_id, path, before, after, writer, *, source_id=None, run_id=None):
    """Add one version to the caller's transaction. Returns None when nothing changed."""
    if before == after: return None
    row = PersonalMemoryVersion(
        id="pamv_" + uuid.uuid4().hex, agent_id=agent_id, path=path, writer=writer,
        source_id=source_id, run_id=run_id, before_hash=digest(before), after_hash=digest(after),
        before_text=before, after_text=after,
        bytes_changed=abs(len((after or "").encode()) - len((before or "").encode())))
    db.add(row)
    return row


def latest(db, agent_id, path):
    """The newest version of one file, or None."""
    return db.scalar(select(PersonalMemoryVersion).where(PersonalMemoryVersion.agent_id == agent_id, PersonalMemoryVersion.path == path)
                     .order_by(PersonalMemoryVersion.created_at.desc(), PersonalMemoryVersion.id.desc()).limit(1))


def listing(db, agent_id, *, path=None, source_id=None, run_id=None, writer=None, since=None, limit=100, offset=0, text=True):
    query = select(PersonalMemoryVersion).where(PersonalMemoryVersion.agent_id == agent_id)
    if path: query = query.where(PersonalMemoryVersion.path == path)
    if source_id: query = query.where(PersonalMemoryVersion.source_id == source_id)
    if run_id: query = query.where(PersonalMemoryVersion.run_id == run_id)
    if writer: query = query.where(PersonalMemoryVersion.writer == writer)
    if since is not None: query = query.where(PersonalMemoryVersion.created_at >= since)
    if not text: query = query.options(defer(PersonalMemoryVersion.before_text), defer(PersonalMemoryVersion.after_text))
    query = query.order_by(PersonalMemoryVersion.created_at.desc(), PersonalMemoryVersion.id.desc())
    return list(db.scalars(query.offset(offset).limit(limit)))


def as_dict(row, *, text=False):
    out = {"id": row.id, "path": row.path, "writer": row.writer, "source_id": row.source_id,
           "run_id": row.run_id, "created": row.before_hash is None, "deleted": row.after_hash is None,
           "bytes_changed": row.bytes_changed, "reverted_by": row.reverted_by,
           "created_at": row.created_at.isoformat() if row.created_at else None}
    if text: out.update(before=row.before_text, after=row.after_text)
    return out


def prune(db, now=None, batch=1000):
    """Drop versions older than KEEP_DAYS, except each file's newest: injection checks a file
    against it (inject.trusted). Called from the background maintenance loop."""
    cutoff = (now or utc_now()) - timedelta(days=KEEP_DAYS)
    newest = select(func.max(PersonalMemoryVersion.created_at)).where(
        PersonalMemoryVersion.agent_id == aliased_row.agent_id, PersonalMemoryVersion.path == aliased_row.path).scalar_subquery()
    rows = db.scalars(select(aliased_row).where(aliased_row.created_at < cutoff, aliased_row.created_at < newest).limit(batch)).all()
    for row in rows: db.delete(row)
    return len(rows)


aliased_row = aliased(PersonalMemoryVersion)
