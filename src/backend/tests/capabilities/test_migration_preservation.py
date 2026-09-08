"""Only exact, rebuildable session copies may be removed during migration."""

from __future__ import annotations

import shutil

import pytest
from core.capabilities import migration, skills


@pytest.fixture()
def layout(tmp_path, monkeypatch, caps_root):
    monkeypatch.setattr(skills, "builtin_dir", lambda: tmp_path / "builtin")
    ws = tmp_path / "workspace"
    source = ws / "skills" / "demo"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("original")
    (source / "resources").mkdir()
    (source / "resources" / "input.bin").write_bytes(b"original-bytes")
    copied = ws / ".sessions" / "one" / "skills"
    shutil.copytree(source, copied / "demo")
    return ws, source, copied


def sweep(ws):
    report = migration.MigrationReport()
    migration._sweep_sessions(report, ws)
    return report


def test_exact_copy_of_preserved_source_can_be_removed(layout):
    ws, source, copied = layout
    report = sweep(ws)
    assert not copied.exists()
    assert source.is_dir()
    assert report.removed_session_copies == [str(copied)]


def test_session_root_regular_file_is_preserved(layout):
    ws, _, copied = layout
    (copied / "user-note.txt").write_text("keep")
    report = sweep(ws)
    assert (copied / "user-note.txt").read_text() == "keep"
    assert str(copied) in report.skipped
    assert not report.removed_session_copies


def test_changed_skill_body_is_preserved(layout):
    ws, _, copied = layout
    (copied / "demo" / "SKILL.md").write_text("user edit")
    report = sweep(ws)
    assert (copied / "demo" / "SKILL.md").read_text() == "user edit"
    assert str(copied) in report.skipped


def test_same_size_binary_modification_is_preserved(layout):
    ws, _, copied = layout
    file = copied / "demo" / "resources" / "input.bin"
    file.write_bytes(b"modified-bytes")
    report = sweep(ws)
    assert file.read_bytes() == b"modified-bytes"
    assert str(copied) in report.skipped


def test_additional_nested_file_is_preserved(layout):
    ws, _, copied = layout
    extra = copied / "demo" / "resources" / "new.txt"
    extra.write_text("keep")
    report = sweep(ws)
    assert extra.read_text() == "keep"
    assert str(copied) in report.skipped


def test_missing_rebuildable_source_preserves_session_copy(layout):
    ws, source, copied = layout
    shutil.rmtree(source)
    report = sweep(ws)
    assert copied.is_dir()
    assert str(copied) in report.skipped


def test_empty_skills_directory_is_preserved(layout):
    ws, _, copied = layout
    shutil.rmtree(copied / "demo")
    report = sweep(ws)
    assert copied.is_dir()
    assert str(copied) in report.skipped


def test_extra_empty_directory_is_preserved(layout):
    ws, _, copied = layout
    (copied / "demo" / "user-empty-directory").mkdir()
    report = sweep(ws)
    assert (copied / "demo" / "user-empty-directory").is_dir()
    assert str(copied) in report.skipped


def test_identical_names_without_skill_contents_do_not_authorize_cleanup(layout):
    ws, _, copied = layout
    (copied / "unrecognized").mkdir()
    report = sweep(ws)
    assert copied.is_dir()
    assert str(copied) in report.skipped


def test_original_view_preserved_in_quarantine_is_valid_source(layout, tmp_path):
    ws, source, copied = layout
    q = tmp_path / "quarantine"
    (q / "view-skills").mkdir(parents=True)
    shutil.move(str(source), str(q / "view-skills" / source.name))
    report = migration.MigrationReport(quarantine_dir=str(q))
    migration._sweep_sessions(report, ws)
    assert not copied.exists()
    assert (q / "view-skills" / "demo" / "SKILL.md").read_text() == "original"
    assert report.removed_session_copies == [str(copied)]


def test_linked_resource_is_preserved_without_following(layout, tmp_path):
    ws, source, copied = layout
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("keep")
    (source / "linked").symlink_to(external, target_is_directory=True)
    (copied / "demo" / "linked").symlink_to(external, target_is_directory=True)
    report = sweep(ws)
    assert copied.is_dir()
    assert (external / "keep.txt").read_text() == "keep"
    assert str(copied) in report.skipped


def test_changed_original_during_verification_blocks_cleanup(layout, monkeypatch):
    ws, source, copied = layout
    read_inventory = migration._tree_inventory
    changed = False

    def mutate_source_after_read(path):
        nonlocal changed
        inventory = read_inventory(path)
        if path == source and not changed:
            changed = True
            (source / "SKILL.md").write_text("source edited concurrently")
        return inventory

    monkeypatch.setattr(migration, "_tree_inventory", mutate_source_after_read)
    report = sweep(ws)
    assert copied.is_dir()
    assert (copied / "demo" / "SKILL.md").read_text() == "original"
    assert "changed during verification" in report.skipped[str(copied)]
