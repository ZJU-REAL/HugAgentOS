import importlib.util
import json
from pathlib import Path

import pytest

from scripts.refactor import rewrite_imports as rewrite


def manifests(tmp_path, monkeypatch, supplemental):
    (tmp_path / "manifest.json").write_text(json.dumps([{"src": "core/old.py", "dst": "features/one/old.py", "batch": "P2-one", "action": "move"}]))
    (tmp_path / "routes.json").write_text(json.dumps({"moves": []}))
    (tmp_path / "supplemental_moves.json").write_text(json.dumps(supplemental))
    monkeypatch.setattr(rewrite, "DATA", tmp_path)


def test_extracted_module_is_included_in_its_own_batch(tmp_path, monkeypatch):
    manifests(tmp_path, monkeypatch, [{"src": "core/extracted.py", "dst": "features/two/extracted.py", "batch": "P2-two", "action": "move"}])
    assert rewrite.load_moves("P2-two") == {"core.extracted": "features.two.extracted"}
    assert rewrite.load_moves("P2-one") == {"core.old": "features.one.old"}


@pytest.mark.parametrize("source,target", [("core/old.py", "features/two/new.py"), ("core/new.py", "features/one/old.py")])
def test_ambiguous_source_or_destination_is_rejected(tmp_path, monkeypatch, source, target):
    manifests(tmp_path, monkeypatch, [{"src": source, "dst": target, "batch": "P2-two", "action": "move"}])
    with pytest.raises(ValueError, match="Conflicting migration path"):
        rewrite.load_moves("P2-two")


def test_reviewed_override_preserves_source_and_batch(tmp_path, monkeypatch):
    manifests(tmp_path, monkeypatch, [{"src": "core/old.py", "dst": "features/one/service.py", "batch": "P2-one", "action": "move", "override": True, "note": "Resolve a module/package collision"}])
    assert rewrite.load_moves("P2-one") == {"core.old": "features.one.service"}


@pytest.mark.parametrize("change", [{"src": "core/missing.py"}, {"batch": "P2-other"}, {"action": "split"}, {"note": ""}])
def test_override_requires_existing_source_same_batch_and_reason(tmp_path, monkeypatch, change):
    entry = {"src": "core/old.py", "dst": "features/one/service.py", "batch": "P2-one", "action": "move", "override": True, "note": "Resolve a module/package collision"}
    entry.update(change)
    manifests(tmp_path, monkeypatch, [entry])
    with pytest.raises(ValueError, match="Invalid reviewed migration override"):
        rewrite.load_moves("P2-one")


def test_module_and_package_destination_collision_is_rejected(tmp_path, monkeypatch):
    manifests(tmp_path, monkeypatch, [{"src": "core/new.py", "dst": "features/one/old/__init__.py", "batch": "P2-two", "action": "move"}])
    with pytest.raises(ValueError, match="Conflicting migration path"):
        rewrite.load_moves("P2-two")
