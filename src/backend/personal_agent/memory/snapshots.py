"""Copies of memory/ taken before each Dreaming run, outside the workspace.

They live next to the workspace (<volume>/<agent>/memory-snapshots/<id>/) so a task in
the sandbox cannot reach them. A failed run restores its own snapshot; the owner can
restore any of the last KEEP.
"""
import json
import os
import re
import shutil
import time

from personal_agent.memory import layout, store, versions
from personal_agent.workspace import files

KEEP = 14
SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _root(ident):
    return files.local_workspace(ident).parent / "memory-snapshots"


def take(ident, snapshot_id, note=""):
    """Copy every memory/*.md; the caller holds the workspace lock."""
    if not SNAPSHOT_ID.match(snapshot_id): raise ValueError("Invalid snapshot id")
    target = _root(ident) / snapshot_id
    if target.exists(): return snapshot_id
    temp = _root(ident) / ("." + snapshot_id + ".tmp")
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True, mode=0o700)
    pages = store.wiki(ident)
    for rel, text in pages.items():
        (temp / rel.split("/", 1)[1]).write_text(text, encoding="utf-8")
    (temp / "manifest.json").write_text(json.dumps({"id": snapshot_id, "at": time.time(), "note": note,
                                                    "files": sorted(pages)}, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, target)
    _prune(ident)
    return snapshot_id


def listing(ident):
    root = _root(ident)
    if not root.is_dir(): return []
    out = []
    for entry in root.iterdir():
        manifest = entry / "manifest.json"
        if entry.name.startswith(".") or not manifest.is_file(): continue
        try: out.append(json.loads(manifest.read_text(encoding="utf-8")))
        except ValueError: continue
    return sorted(out, key=lambda item: item.get("at", 0), reverse=True)


def restore(db, agent, snapshot_id, *, run_id=None):
    """Make memory/ exactly the snapshot again; each changed file gets a SYSTEM version.

    The caller holds the workspace lock (Dreaming rollback) or none (owner restore)."""
    if not SNAPSHOT_ID.match(snapshot_id): raise ValueError("Invalid snapshot id")
    source = _root(agent.id) / snapshot_id
    if not (source / "manifest.json").is_file(): raise LookupError("Snapshot not found")
    # _schema.md is the platform's, never an old copy of it (seed keeps it current).
    wanted = {layout.DIR + "/" + p.name: p.read_text(encoding="utf-8") for p in source.iterdir()
              if p.name.endswith(".md") and layout.DIR + "/" + p.name != layout.SCHEMA}
    current = {rel: text for rel, text in store.wiki(agent.id).items() if rel != layout.SCHEMA}
    # Writes first, deletions last: a failure part way never leaves pages deleted and unversioned.
    for rel, text in wanted.items():
        if current.get(rel) == text: continue
        files._write_file(agent.id, rel, text, None)
        versions.record(db, agent.id, rel, current.get(rel), text, layout.SYSTEM, run_id=run_id)
    fd = files._open_dir(files.local_workspace(agent.id), [layout.DIR], create=True)
    try:
        for rel in sorted(set(current) - set(wanted)):
            os.unlink(rel.split("/", 1)[1], dir_fd=fd)
            versions.record(db, agent.id, rel, current[rel], None, layout.SYSTEM, run_id=run_id)
        os.fsync(fd)
    finally:
        os.close(fd)


def _prune(ident):
    for item in listing(ident)[KEEP:]:
        shutil.rmtree(_root(ident) / item["id"], ignore_errors=True)
