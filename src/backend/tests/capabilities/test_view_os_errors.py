"""OS link failures are reported without deleting source bytes or copying views."""

import errno
from pathlib import Path
import pytest
from core.capabilities import junction, view
from core.capabilities.errors import ViewUnavailable


def locked(path):
    error = PermissionError(errno.EACCES, "resource is in use", str(path))
    error.winerror = 32
    return error


@pytest.fixture
def existing_view(tmp_path):
    source = tmp_path / "store"
    v1, v2 = source / "v1", source / "v2"
    for folder, body in ((v1, "version-one"), (v2, "version-two")):
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(body)
    directory = tmp_path / "view"
    link = directory / "report"
    junction.create_directory_link(link, v1, allowed_roots=[source])
    return source, v1, v2, directory, link


def deny_unlink(monkeypatch, link):
    original = junction.os.unlink

    def unlink(path, *args, **kwargs):
        if Path(path) == link:
            raise locked(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(junction.os, "unlink", unlink)


def test_remove_wraps_busy_os_error_and_keeps_existing_link(existing_view, monkeypatch):
    source, v1, v2, directory, link = existing_view
    deny_unlink(monkeypatch, link)
    with pytest.raises(junction.LinkError) as caught:
        junction.remove_directory_link(link)
    assert caught.value.__cause__.winerror == 32
    assert junction.read_directory_link(link) == v1
    assert (link / "SKILL.md").read_text() == "version-one"


@pytest.mark.parametrize("replacement", [False, True], ids=["stale_link_cleanup", "replace_link"])
def test_view_reports_busy_link_and_retry_succeeds_after_unlock(
    existing_view, monkeypatch, replacement
):
    source, v1, v2, directory, link = existing_view
    with monkeypatch.context() as patch:
        deny_unlink(patch, link)
        report = view.build_view(
            directory, {"report": v2} if replacement else {}, allowed_roots=[source]
        )
        assert "report" in report.blocked
        assert "resource is in use" in report.blocked["report"]
        assert not report.changed
        assert junction.read_directory_link(link) == v1
        assert (link / "SKILL.md").read_text() == "version-one"
    report = view.build_view(
        directory, {"report": v2} if replacement else {}, allowed_roots=[source]
    )
    assert not report.blocked
    assert report.relinked == ["report"] if replacement else report.removed == ["report"]
    assert (v1 / "SKILL.md").read_text() == "version-one"
    assert (v2 / "SKILL.md").read_text() == "version-two"


def test_link_inspection_error_is_structured(existing_view, monkeypatch):
    source, v1, v2, directory, link = existing_view
    monkeypatch.setattr(junction, "_lstat", lambda path: (_ for _ in ()).throw(locked(path)))
    with pytest.raises(junction.LinkError):
        junction.is_directory_link(link)
    report = view.build_view(directory, {"report": v2}, allowed_roots=[source])
    assert "report" in report.blocked and not report.changed


def test_view_root_permission_error_is_view_unavailable(tmp_path, monkeypatch):
    root = tmp_path / "denied-view"
    original = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path == root:
            raise locked(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(ViewUnavailable) as caught:
        view.build_view(root, {}, allowed_roots=[tmp_path])
    assert caught.value.to_dict()["code"] == "view_unavailable"
    assert caught.value.retryable


def test_parent_creation_error_is_link_error_without_copy(tmp_path, monkeypatch):
    target = tmp_path / "source"
    target.mkdir()
    (target / "SKILL.md").write_text("original")
    parent = tmp_path / "denied-view"
    original = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path == parent:
            raise locked(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(junction.LinkError):
        junction.create_directory_link(parent / "report", target, allowed_roots=[tmp_path])
    assert not parent.exists()
    assert (target / "SKILL.md").read_text() == "original"
