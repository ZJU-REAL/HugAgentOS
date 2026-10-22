"""Test moves preserve exact failure identities without masking regressions."""
import pytest
from scripts.refactor.check_test_baseline import canonical_failures

OLD = "src/backend/tests/services/test_source.py"
NEW = "src/backend/tests/features/datasources/test_source.py"


def test_only_file_prefix_is_normalized():
    node = NEW + "::test_query[" + NEW + "]"
    assert canonical_failures([node], {OLD: NEW}) == {OLD + "::test_query[" + NEW + "]"}


def test_new_tests_remain_new_failures():
    assert canonical_failures([NEW + "::test_new"], {OLD: NEW}) == {OLD + "::test_new"}


@pytest.mark.parametrize("moves", [
    {OLD: OLD},
    {OLD: NEW, "src/backend/tests/other.py": NEW},
    {OLD: NEW, NEW: "src/backend/tests/final.py"},
    {OLD: "src/backend/tests/../app.py"},
    {OLD: "src/backend/api/app.py"},
])
def test_ambiguous_or_non_test_mappings_are_rejected(moves):
    with pytest.raises(ValueError):
        canonical_failures([], moves)


def test_dependency_baseline_keeps_original_test_identity(tmp_path, monkeypatch):
    import json
    from scripts.refactor import import_contract
    (tmp_path / "manifest.json").write_text("[]")
    (tmp_path / "test_moves.json").write_text(json.dumps({OLD: NEW}))
    monkeypatch.setattr(import_contract, "DATA", tmp_path)
    assert import_contract.canonical_names()["tests.features.datasources.test_source"] == "tests.services.test_source"


def test_route_moves_are_in_dependency_baseline(tmp_path, monkeypatch):
    import json
    from scripts.refactor import import_contract
    (tmp_path / "manifest.json").write_text("[]")
    (tmp_path / "routes.json").write_text(json.dumps({"moves": [{"src": "api/routes/v1/sites.py", "dst": "features/sites/api/sites.py"}]}))
    monkeypatch.setattr(import_contract, "DATA", tmp_path)
    assert import_contract.canonical_names()["api.routes.v1.sites"] == "features.sites.api.sites"
