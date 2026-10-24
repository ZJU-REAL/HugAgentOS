"""Behavioral checks for the refactoring gates."""
from pathlib import Path
from types import SimpleNamespace

from scripts.refactor.check_ce_paths import path_errors
from scripts.refactor.check_line_budget import violations
from scripts.refactor.import_contract import edge_rule, layer
from scripts.refactor.snapshot_routes import snapshot


def test_routes_keep_duplicates_methods_and_registration_order():
    def route(path, methods):
        return SimpleNamespace(path=path, methods=set(methods))
    app = SimpleNamespace(
        routes=[route("/a", ["POST", "GET"]), route("/a", ["GET"]), route("/b", [])],
        openapi=lambda: {"paths": {"/b": {}, "/a": {}}},
    )
    result = snapshot(app)
    assert result["openapi_paths"] == ["/a", "/b"]
    assert [r["path"] for r in result["routes"]] == ["/a", "/a", "/b"]
    assert [r["order"] for r in result["routes"]] == [0, 1, 2]
    assert result["routes"][0]["methods"] == ["GET", "POST"]


def test_line_budget_blocks_growth_but_allows_reduction(tmp_path):
    path = tmp_path / "src/backend/core/example.py"
    path.parent.mkdir(parents=True)
    path.write_text("# line\n" * 600)
    key = "src/backend/core/example.py"
    assert violations(tmp_path, {key: 600}) == []
    assert violations(tmp_path, {key: 601}) == []
    assert len(violations(tmp_path, {key: 599})) == 1
    assert len(violations(tmp_path, {})) == 1


def test_new_file_at_limit_passes(tmp_path):
    path = tmp_path / "src/backend/new.py"
    path.parent.mkdir(parents=True)
    path.write_text("# line\n" * 500)
    assert violations(tmp_path, {}) == []


def test_ce_missing_business_path_is_not_silently_ignored(tmp_path):
    manifest = {"overlay": "overlay", "exclude": ["src/missing.py"]}
    assert path_errors(tmp_path, manifest, []) == [
        "exclude: no source file matches src/missing.py"
    ]


def test_ce_safety_exception_retains_exclusion(tmp_path):
    manifest = {
        "overlay": "overlay", "exclude": ["private.key"],
        "optional_safety_excludes": ["private.key"],
    }
    assert path_errors(tmp_path, manifest, []) == []
    manifest["exclude"] = []
    assert path_errors(tmp_path, manifest, []) == [
        "Safety exception has no exclusion: private.key"
    ]


def test_ce_required_overlay_and_prune_input_are_different_phases(tmp_path):
    overlay = tmp_path / "overlay/src"
    overlay.mkdir(parents=True)
    (overlay / "only.py").write_text("")
    manifest = {
        "overlay": "overlay", "required_files": ["src/only.py"],
        "split": ["src/only.py"], "prunes": {"example": {"path": "src/only.py"}},
    }
    assert path_errors(tmp_path, manifest, []) == [
        "prunes.example: missing source src/only.py"
    ]


def test_dependency_directions_cover_feature_and_core_boundaries():
    assert edge_rule("core.llm.runner", "features.desktop.service") == "core-outward"
    assert edge_rule("features.desktop.service", "features.sites.service") == "feature-direction"
    assert edge_rule("features.sites.service", "features.desktop.service") is None
    assert edge_rule("core.db.engine", "core.models.client") == "core-layers"
    assert edge_rule("core.models.client", "core.db.engine") is None


def test_nested_command_layer_takes_priority_over_sandbox():
    assert layer("core.sandbox.commands.runtime") == 1
    assert layer("core.sandbox.providers.local") == 4
    assert edge_rule("core.llm.tools", "core.sandbox.commands.runtime") == "core-layers"


def test_import_contract_rejects_new_edge_even_when_existing_debt_is_allowed(monkeypatch, tmp_path):
    import json
    import scripts.refactor.import_contract as contract

    (tmp_path / "import-baseline.json").write_text(json.dumps(["existing"]))
    monkeypatch.setattr(contract, "DATA", tmp_path)
    monkeypatch.setattr(contract, "violations", lambda graph: {"existing", "new-edge"})
    gate = contract.CoreBoundaryContract("test", {}, {})
    check = gate.check(None, False)
    assert not check.kept
    assert check.metadata["introduced"] == ["new-edge"]
    assert check.ignored_import_count == 1


def test_deleted_tracked_file_cannot_satisfy_ce_path_gate(tmp_path):
    manifest = {"overlay": "overlay", "exclude": ["src/business.py"]}
    assert path_errors(tmp_path, manifest, ["src/business.py"]) == [
        "exclude: no source file matches src/business.py"
    ]


def test_ce_overlay_cannot_restore_moved_repository_exports(tmp_path):
    from scripts.refactor.check_ce_paths import path_errors
    path = tmp_path / "overlay/src/backend/core/db/repository/__init__.py"
    path.parent.mkdir(parents=True)
    path.write_text("from integrations.email.repository import EmailConnectionRepository\n")
    errors = path_errors(tmp_path, {"overlay": "overlay"}, [])
    assert len(errors) == 1 and "forwards moved ownership" in errors[0]
    path.write_text("from core.db.repository.user import UserRepository\n")
    assert path_errors(tmp_path, {"overlay": "overlay"}, []) == []


def test_canonical_does_not_repeat_nested_destination():
    from scripts.refactor.import_contract import canonical
    moves = {"core.evolution.settlement": "core.evolution.settlement.settlement",
             "core.evolution.settlement_runner": "core.evolution.settlement.settlement_runner"}
    assert canonical("core.evolution.settlement", moves) == "core.evolution.settlement.settlement"
    assert canonical("core.evolution.settlement.settlement", moves) == "core.evolution.settlement.settlement"
    assert canonical("core.evolution.settlement.settlement_runner", moves) == "core.evolution.settlement.settlement_runner"
