"""Continuation must avoid rescanning completed subtrees."""
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("scan", Path(__file__).resolve().parents[2] / "core/llm/tools/glob_scan.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_cursor_visits_each_file_once_without_reopening_finished_subtrees(tmp_path, monkeypatch):
    for folder in ("a", "b", "c"):
        (tmp_path / folder).mkdir()
        for name in ("one.txt", "two.txt"):
            (tmp_path / folder / name).write_text(name)
    first = module.scan(str(tmp_path), "**/*.txt", 2)
    assert first["has_more"]
    visits = []
    original = module.os.scandir
    def observe(path):
        visits.append(str(path))
        return original(path)
    monkeypatch.setattr(module.os, "scandir", observe)
    second = module.scan(str(tmp_path), "**/*.txt", 2, first["next_cursor"])
    third = module.scan(str(tmp_path), "**/*.txt", 2, second["next_cursor"])
    assert str(tmp_path / "a") not in visits[-2:]
    assert str(tmp_path / "a") not in visits[visits.index(str(tmp_path), 1):]
    files = first["filenames"] + second["filenames"] + third["filenames"]
    assert len(set(files)) == 6
    assert third["total"] == 6
    assert third["next_cursor"] is None


def test_matching_names_and_live_changes(tmp_path):
    (tmp_path / "root.txt").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "line\nbreak.txt").write_text("")
    (tmp_path / "link").symlink_to(tmp_path / "sub", target_is_directory=True)
    assert module.scan(str(tmp_path), "*.txt")["num_files"] == 1
    assert module.scan(str(tmp_path), "**/*.txt")["num_files"] == 2
    first = module.scan(str(tmp_path), "**/*.txt", 1)
    (tmp_path / "sub" / "z.txt").write_text("new")
    following = module.scan(str(tmp_path), "**/*.txt", 10, first["next_cursor"])
    assert any(p.endswith("z.txt") for p in following["filenames"])
    with pytest.raises(ValueError):
        module.scan(str(tmp_path), "**/*.py", 2, first["next_cursor"])


def test_cursor_rejects_escape(tmp_path):
    import base64, json, hashlib
    query = hashlib.sha256(json.dumps([str(tmp_path), "**/*"]).encode()).hexdigest()
    cursor = base64.urlsafe_b64encode(json.dumps({"query":query,"after":["..","secret"],"count":1}).encode()).decode()
    with pytest.raises(ValueError):
        module.scan(str(tmp_path), "**/*", 2, cursor)
