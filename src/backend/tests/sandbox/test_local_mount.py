"""Ticket #04: local-project workspace mounting + real-folder listing.

Pure filesystem unit tests using tmp dirs. Edition-agnostic shared module, so it
runs directly in the main tree.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_MOD = os.path.normpath(os.path.join(_HERE, "..", "..", "core", "sandbox", "local_mount.py"))
_spec = importlib.util.spec_from_file_location("local_mount", _MOD)
lm = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lm
_spec.loader.exec_module(lm)  # type: ignore[union-attr]


def test_ensure_link_creates_symlink_to_real_folder(tmp_path):
    real = tmp_path / "proj"
    real.mkdir()
    ws = tmp_path / "ws"
    link = lm.ensure_local_project_link("myproj", str(real), workspace_root=str(ws))
    assert os.path.islink(link)
    assert os.path.realpath(link) == os.path.realpath(str(real))
    assert link == str(ws / "local" / "myproj")


def test_ensure_link_is_idempotent(tmp_path):
    real = tmp_path / "proj"
    real.mkdir()
    ws = tmp_path / "ws"
    a = lm.ensure_local_project_link("s", str(real), workspace_root=str(ws))
    b = lm.ensure_local_project_link("s", str(real), workspace_root=str(ws))
    assert a == b and os.path.islink(b)


def test_ensure_link_repoints_when_target_changes(tmp_path):
    r1 = tmp_path / "p1"
    r1.mkdir()
    r2 = tmp_path / "p2"
    r2.mkdir()
    ws = tmp_path / "ws"
    lm.ensure_local_project_link("s", str(r1), workspace_root=str(ws))
    link = lm.ensure_local_project_link("s", str(r2), workspace_root=str(ws))
    assert os.path.realpath(link) == os.path.realpath(str(r2))


def test_ensure_link_missing_folder_raises(tmp_path):
    ws = tmp_path / "ws"
    with pytest.raises(ValueError):
        lm.ensure_local_project_link("s", str(tmp_path / "nope"), workspace_root=str(ws))


def test_remove_link_leaves_real_folder(tmp_path):
    real = tmp_path / "proj"
    real.mkdir()
    (real / "keep.txt").write_text("data")
    ws = tmp_path / "ws"
    lm.ensure_local_project_link("s", str(real), workspace_root=str(ws))
    lm.remove_local_project_link("s", workspace_root=str(ws))
    assert not os.path.exists(str(ws / "local" / "s"))
    assert (real / "keep.txt").read_text() == "data"  # real files untouched


def test_list_local_files_skips_noise_and_lists_relative(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("x")
    (root / "README.md").write_text("hi")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("noise")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("noise")
    (root / ".env").write_text("secret")
    items = lm.list_local_files(str(root))
    paths = sorted(i["path"] for i in items)
    # paths are normalized to POSIX separators on every platform
    assert paths == ["README.md", "src/main.py"]
    assert all(i["mtime"] for i in items)


def test_list_local_files_respects_limit(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    for i in range(10):
        (root / f"f{i}.txt").write_text("x")
    assert len(lm.list_local_files(str(root), limit=3)) == 3


def test_list_local_files_missing_dir_is_empty():
    assert lm.list_local_files("/no/such/dir/xyz") == []
