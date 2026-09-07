"""Stored package files stay inside their root and retain portable file names."""

import io
import stat
import zipfile
import pytest
from core.capabilities import archive, junction, store
from core.capabilities.errors import IntegrityFailed


def test_existing_directory_link_still_checks_allowed_roots(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    link = root / "view"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(junction.LinkTargetOutsideRoot):
        junction.ensure_directory_link(link, outside, allowed_roots=[root])
    assert link.is_symlink()


def test_failed_link_replacement_keeps_original(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    good = root / "good"
    good.mkdir()
    link = root / "view"
    link.symlink_to(good, target_is_directory=True)
    outside = tmp_path / "other"
    outside.mkdir()
    with pytest.raises(junction.LinkTargetOutsideRoot):
        junction.ensure_directory_link(link, outside, allowed_roots=[root])
    assert link.resolve() == good


@pytest.mark.parametrize("level", ["skills", "skills/local", "skills/local/item"])
def test_store_rejects_redirected_parent(tmp_path, monkeypatch, level):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    link = root / level
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(root))
    with pytest.raises(IntegrityFailed):
        store.write_from_files("skill", "local", "item", "a" * 12, {"SKILL.md": "content"})
    assert list(outside.iterdir()) == []


def test_archive_accepts_unicode_and_spaces(tmp_path):
    name = "references/\u6570\u636e report.md"
    archive.write_files(tmp_path / "files", {"SKILL.md": "x", name: "test"})
    assert (tmp_path / "files" / name).read_text() == "test"


@pytest.mark.parametrize(
    "files",
    [
        {"SKILL.md": "x", "A.py": "1", "a.py": "2"},
        {"SKILL.md": "x", "Dir/a": "1", "dir/b": "2"},
        {"SKILL.md": "x", "a": "1", "a/b": "2"},
        {"SKILL.md": "x", "a/../b": "1"},
        {"SKILL.md": "x", "a.": "1"},
    ],
)
def test_file_map_rejects_nonportable_collisions(tmp_path, files):
    with pytest.raises(IntegrityFailed):
        archive.write_files(tmp_path / "files", files)


def test_zip_directory_link_is_not_a_regular_file(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", "x")
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "other")
    with pytest.raises(IntegrityFailed):
        archive.extract_zip(buf.getvalue(), tmp_path / "files")


def test_iter_files_rejects_linked_content(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("text")
    (root / "linked.txt").symlink_to(outside)
    with pytest.raises(IntegrityFailed):
        list(archive.iter_files(root))


def test_duplicate_zip_member_is_rejected(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", "one")
        z.writestr("SKILL.md", "two")
    with pytest.raises(IntegrityFailed):
        archive.extract_zip(buf.getvalue(), tmp_path / "files")
