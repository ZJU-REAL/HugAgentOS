"""Ticket #08: local write-back snapshots + rollback. Temp HUGAGENT_HOME."""

from __future__ import annotations

import importlib


def _svc(tmp_path, monkeypatch):
    monkeypatch.setenv("HUGAGENT_HOME", str(tmp_path / "home"))
    import core.services.local_snapshot_service as s

    return importlib.reload(s)


def test_snapshot_then_rollback_restores_previous(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    f = tmp_path / "doc.txt"
    f.write_text("v1")
    # snapshot v1, then the agent overwrites to v2
    assert svc.snapshot(str(f)) is not None
    f.write_text("v2")
    assert svc.rollback(str(f)) is True
    assert f.read_text() == "v1"


def test_snapshot_of_missing_file_is_noop(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    assert svc.snapshot(str(tmp_path / "nope.txt")) is None


def test_rollback_without_snapshot_returns_false(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    f = tmp_path / "x.txt"
    f.write_text("only")
    assert svc.rollback(str(f)) is False


def test_list_snapshotted_files(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    f = tmp_path / "a.txt"
    f.write_text("1")
    svc.snapshot(str(f))
    files = svc.list_snapshotted_files()
    assert any(item["path"].endswith("a.txt") and item["count"] >= 1 for item in files)


def test_snapshots_are_pruned(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    f = tmp_path / "b.txt"
    for i in range(15):
        f.write_text(f"v{i}")
        svc.snapshot(str(f))
    assert len(svc.list_snapshots(str(f))) <= 10  # _MAX_PER_FILE
