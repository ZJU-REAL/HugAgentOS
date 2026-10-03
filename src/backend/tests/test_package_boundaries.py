"""Keep plugin lifecycle independent from execution assembly after packaging."""

import ast
import importlib.util
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def imported_modules(path):
    module = ".".join(path.relative_to(BACKEND).with_suffix("").parts)
    package = module.rsplit(".", 1)[0]
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            yield base
            yield from (base + "." + alias.name for alias in node.names)


@pytest.mark.parametrize("layer", ["management", "packaging"])
def test_plugin_lifecycle_does_not_depend_on_agent_assembly(layer):
    forbidden = ("core.llm.factory", "core.plugins.runtime")
    violations = [
        f"{path.relative_to(BACKEND)}: {name}"
        for path in (BACKEND / "core/plugins" / layer).rglob("*.py")
        for name in imported_modules(path)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]
    assert violations == []


def test_archive_and_manifest_parsing_do_not_depend_on_database_lifecycle():
    forbidden = ("core.db", "core.plugins.management", "sqlalchemy")
    names = ("archive.py", "importer.py", "models.py")
    violations = [
        f"{filename}: {name}"
        for filename in names
        for name in imported_modules(BACKEND / "core/plugins/packaging" / filename)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]
    assert violations == []


def test_packaged_plugin_discovery_keeps_bundled_resources():
    from core.plugins.packaging.definitions import PLUGIN_BUNDLES_DIR
    from core.plugins.packaging.importer import normalize_plugin_dir
    from core.plugins.packaging.sources import _resolve_plugin_dir

    assert PLUGIN_BUNDLES_DIR == BACKEND / "plugin_bundles"
    plugin_dir = _resolve_plugin_dir("skill-manager")
    assert plugin_dir is not None
    normalized = normalize_plugin_dir(plugin_dir)
    assert normalized.slug == "skill-manager"
    assert normalized.skills


def test_ce_distribution_excludes_enterprise_cube_package():
    import fnmatch
    import yaml

    repo = BACKEND.parents[1]
    modules = list((BACKEND / "core/sandbox/cube").glob("*.py"))
    manifest_path = repo / "ce/manifest.yaml"
    if not manifest_path.exists():
        assert not modules, "A derived CE tree must not contain the Cube package"
        return
    manifest = yaml.safe_load(manifest_path.read_text())
    assert modules, "Source checkout must include the EE package to verify its exclusion"
    assert all(
        any(fnmatch.fnmatch(str(path.relative_to(repo)), rule) for rule in manifest["exclude"])
        for path in modules
    )
