"""One sync of one agent: diff the plan against the manifests, copy what changed, remove what is gone,
write the manifests and record capability_sync in the agent's config.

The workspace is the agent's own: a directory the manifest does not name belongs to the agent and
is never touched. When such a directory has the id of a platform skill, the agent's copy wins and
the platform one is reported as skipped (unless the two are identical, which happens when a sync
stopped between renaming a copy into place and writing the manifest: then it is adopted).
"""
import logging
import time

from personal_agent.capability_sync import plan as plans
from personal_agent.capability_sync import store
from personal_agent.capability_sync import tree as trees

logger = logging.getLogger(__name__)


def sync(db, user_id, *, sources=None):
    """Bring the owner's workspace in line with their binding and record the outcome.
    Returns the capability_sync status, or None when the owner has no agent. Blocking: call it from a
    worker thread or a sync route, never on the event loop."""
    from sqlalchemy import select
    from personal_agent.identity.config import update_config
    from personal_agent.models import PersonalAgent
    from personal_agent.workspace.files import WorkspaceUnavailable
    row = db.scalar(select(PersonalAgent).where(PersonalAgent.user_id == str(user_id)))
    if row is None: return None
    agent_id, owner, config = row.id, row.user_id, dict(row.config or {})
    try:
        chosen = plans.selection(db, owner, config)
        records = plans.plugin_records(db, [p["id"] for p in chosen["plugins"]])
        # The read is over: end the transaction so no database lock is held while files are copied.
        db.rollback()
        status = apply(agent_id, plans.build(chosen, records, sources))
    except Exception as exc:  # noqa: BLE001 - a failed copy is reported, it never fails the caller
        expected = isinstance(exc, (plans.CatalogUnavailable, WorkspaceUnavailable, OSError))
        (logger.warning if expected else logger.exception)("personal agent capability sync failed agent=%s: %s", agent_id, exc)
        db.rollback()
        status = {"status": "failed", "skipped": [],
                  "error": plans.reason(exc) if expected else "Skills could not be copied to the workspace; try again"}
    status = {"status": status["status"], "synced_at": time.time(), "error": status["error"], "skipped": status["skipped"]}
    update_config(db, owner, lambda config: config.update(capability_sync=status))
    return status


def apply(agent_id, plan):
    """Write the plan into the workspace. Returns {"status", "error", "skipped"}."""
    errors, skipped = [], []
    with store.agent_lock(agent_id):
        for kind, wanted in ((store.SKILLS, plan.skills), (store.PLUGINS, plan.plugins)):
            with store.opened(agent_id, kind) as fd:
                store.sweep_staged(fd)
                previous = store.read_manifest(fd, kind)
                entries = _reconcile(fd, previous, wanted, errors, skipped)
                if entries != previous or store.kind_of(fd, store.MANIFEST) is None:
                    store.write_manifest(fd, kind, entries)
    return {"status": "failed" if errors else "ok", "error": "; ".join(errors) or None, "skipped": skipped}


def _reconcile(fd, previous, wanted, errors, skipped):
    entries = {}
    # Removed first: a binding change that swaps one skill for another never holds both.
    for name, recorded in previous.items():
        if name in wanted: continue
        try: store.remove(fd, name)
        except OSError as exc:
            errors.append(name + ": " + plans.reason(exc))
            entries[name] = recorded
    present = store.names(fd)
    for name, entry in wanted.items():
        recorded = previous.get(name)
        if not store.valid_name(name):
            skipped.append({"id": name, "reason": "invalid_id"})
            continue
        if entry.error:
            if entry.error == "unavailable": skipped.append({"id": name, "reason": "unavailable"})
            else: errors.append(name + ": " + entry.error)
            # The last good copy stays until its source can be read again.
            if recorded is not None and name in present: entries[name] = recorded
            continue
        record = {"source": entry.source, "hash": entry.hash} if entry.source != "plugin" else {**entry.extra, "hash": entry.hash}
        if recorded is None and name in present:
            if store.kind_of(fd, name) != "dir" or not _identical(fd, name, entry.hash):
                skipped.append({"id": name, "reason": "name_taken"})
                continue
            entries[name] = record
            continue
        if recorded is not None and recorded.get("hash") == entry.hash and store.kind_of(fd, name) == "dir":
            entries[name] = record
            continue
        try:
            store.replace(fd, name, entry.tree)
            entries[name] = record
        except (OSError, ValueError) as exc:
            errors.append(name + ": " + plans.reason(exc))
            if recorded is not None and name in present: entries[name] = recorded
    return entries


def _identical(fd, name, expected):
    try: return trees.digest(trees.read_at(fd, name)) == expected
    except (OSError, ValueError): return False
