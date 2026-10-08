"""A migration cannot silently replace unsynchronized project edits."""

import hashlib
from types import SimpleNamespace

import pytest
from core.services.application_site_sources import validate_mirrors


def test_dirty_mirror_is_preserved(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    folder = root / "team_workspaces" / "example" / "files"
    folder.mkdir(parents=True)
    target = folder / "App.jsx"
    target.write_bytes(b"user edit")
    monkeypatch.setattr(
        "edition_ee.services.team_workspace.read_manifest",
        lambda _: {"App.jsx": {"sha256": hashlib.sha256(b"original").hexdigest()}},
    )
    with pytest.raises(RuntimeError, match="unsynchronized edits"):
        validate_mirrors(
            [SimpleNamespace(root=folder)], [("App.jsx", b"converted")], root / "backup", root
        )
    assert target.read_bytes() == b"user edit"


def test_clean_mirror_backup_is_immutable(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    folder = root / "team_workspaces" / "example" / "files"
    folder.mkdir(parents=True)
    target = folder / "App.jsx"
    target.write_bytes(b"original")
    monkeypatch.setattr(
        "edition_ee.services.team_workspace.read_manifest",
        lambda _: {"App.jsx": {"sha256": hashlib.sha256(b"original").hexdigest()}},
    )
    backup = root / "backup"
    validate_mirrors([SimpleNamespace(root=folder)], [("App.jsx", b"converted")], backup, root)
    original = backup / "sources" / target.relative_to(root)
    assert original.read_bytes() == b"original"
    target.write_bytes(b"converted")
    validate_mirrors([SimpleNamespace(root=folder)], [("App.jsx", b"converted")], backup, root)
    assert original.read_bytes() == b"original"
