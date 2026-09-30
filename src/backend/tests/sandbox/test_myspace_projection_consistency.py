"""Files at the same logical path must agree after a synchronization barrier."""
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tests.sandbox.test_myspace_mirror_sync import mirror, _pull_db, _row  # noqa: F401
from core.myspace import projection as pr, reconciliation as rc, mirror as mm


def test_overwrite_within_two_seconds_is_visible(mirror, monkeypatch):
    now = datetime.now(timezone.utc)
    path = mirror / "report.txt"
    path.write_bytes(b"old")
    os.utime(path, (now.timestamp()-0.5, now.timestamp()-0.5))
    from core.space_sync.index import remember
    from core.space_sync.content import signature
    import hashlib
    remember("u1", "report.txt", {"id": None, "key": "prior/cloud/version", "sha256": hashlib.sha256(b"old").hexdigest(), "signature": signature(path), "directory": False})
    _pull_db([_row("report.txt", size=3, updated=now)], monkeypatch)
    report = pr.pull_myspace_updates(user_id="u1")
    assert report.failed == 0
    assert path.read_bytes() == b"new"


def test_deleted_old_identity_never_removes_new_identity(mirror, monkeypatch):
    now = datetime.now(timezone.utc)
    path = mirror / "report.txt"
    path.write_bytes(b"new")
    old = _row("report.txt", size=3, updated=now-timedelta(seconds=10), deleted_at=now)
    live = _row("report.txt", size=3, updated=now-timedelta(seconds=1))
    _pull_db([old, live], monkeypatch)
    report = pr.pull_myspace_updates(user_id="u1")
    assert report.removed == 0
    assert path.read_bytes() == b"new"


def test_reverse_sync_detects_subsecond_edit():
    now = datetime.now(timezone.utc)
    art = _row("rapid.txt", size=3, updated=now)
    entry = mm.MirrorEntry(rel="rapid.txt", path=None, size=3, mtime=now.timestamp() + 0.01)
    assert not mm._artifact_is_current(art, entry)
