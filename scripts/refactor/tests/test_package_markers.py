from scripts.refactor.import_contract import package_marker_errors


def test_missing_domain_and_nested_package_cannot_escape_scan(tmp_path):
    nested = tmp_path / "features/marketplace/versions"
    nested.mkdir(parents=True)
    (nested / "selection.py").write_text("value = 1")
    assert package_marker_errors(tmp_path) == {
        "package-marker: features", "package-marker: features/marketplace",
        "package-marker: features/marketplace/versions",
    }
    for path in (tmp_path / "features", nested.parent, nested):
        (path / "__init__.py").write_text("")
    assert package_marker_errors(tmp_path) == set()


def test_non_domain_scripts_do_not_need_package_markers(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/job.py").write_text("pass")
    assert package_marker_errors(tmp_path) == set()
