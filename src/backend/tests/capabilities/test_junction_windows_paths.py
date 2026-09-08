"""Windows path spelling checks; real junction behavior is exercised on Windows."""

import os
import pytest
from core.capabilities import junction
from core.capabilities.junction import _extended_windows_path, _strip_windows_prefix


def test_drive_path_adds_extended_prefix_and_roundtrips():
    raw = r"C:\Users\Aaron\long directory\data"
    native = _extended_windows_path(raw)
    assert native == "\\\\?\\" + raw
    assert _strip_windows_prefix(native) == raw


def test_unc_keeps_network_server_and_share():
    raw = r"\\server\share\long directory"
    native = _extended_windows_path(raw)
    assert native == r"\\?\UNC\server\share\long directory"
    assert _strip_windows_prefix(native) == raw


def test_existing_extended_prefix_is_not_duplicated():
    raw = r"\\?\C:\directory\file"
    assert _extended_windows_path(raw) == raw


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows junctions")
def test_long_target_is_readable_without_long_paths_policy(tmp_path):
    root = tmp_path / "allowed"
    target = root / ("a" * 100) / ("b" * 100) / ("c" * 100)
    native = junction._native(target)
    native.mkdir(parents=True)
    (native / "sentinel.txt").write_text("preserved")
    link = tmp_path / "view"
    junction.create_directory_link(link, target, allowed_roots=[root])
    assert (link / "sentinel.txt").read_text() == "preserved"
    assert junction.ensure_directory_link(link, target, allowed_roots=[root]) is False
    junction.remove_directory_link(link)
    assert (native / "sentinel.txt").read_text() == "preserved"


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows junctions")
def test_long_link_can_be_replaced_and_removed(tmp_path):
    root = tmp_path / "allowed"
    first, second = root / "first", root / "second"
    first.mkdir(parents=True)
    second.mkdir()
    (second / "sentinel.txt").write_text("preserved")
    link = tmp_path / ("x" * 100) / ("y" * 100) / ("z" * 100) / "view"
    junction.create_directory_link(link, first, allowed_roots=[root])
    assert junction.ensure_directory_link(link, second, allowed_roots=[root])
    assert junction._native(link / "sentinel.txt").read_text() == "preserved"
    junction.remove_directory_link(link)
    assert not junction.is_directory_link(link)
    assert (second / "sentinel.txt").read_text() == "preserved"


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows path semantics")
def test_extended_prefix_cannot_bypass_allowed_root(tmp_path):
    root, outside = tmp_path / "allowed", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    with pytest.raises(junction.LinkTargetOutsideRoot):
        junction.create_directory_link(
            tmp_path / "bad", junction._native(outside), allowed_roots=[root]
        )
