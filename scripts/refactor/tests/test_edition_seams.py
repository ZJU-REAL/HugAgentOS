"""Enforce the completed P7 boundary instead of relying on reviewer memory."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_core_and_feature_overlays_select_only_edition_policy():
    overlay = ROOT / "ce/overlay/src/backend"
    violations = []
    for domain in ("core", "features"):
        for path in (overlay / domain).rglob("*.py"):
            if path == overlay / "features/installed.py":
                continue  # D10's installed-module selection is explicitly overlaid.
            if path.name != "edition.py" or len(path.read_text().splitlines()) > 150:
                violations.append(str(path.relative_to(ROOT)))
    assert violations == []


def test_enterprise_imports_are_confined_to_edition_entry_points():
    violations = []
    backend = ROOT / "src/backend"
    for domain in ("core", "features", "integrations", "api", "orchestration", "personal_agent"):
        for path in (backend / domain).rglob("*.py"):
            if "edition" in path.stem:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                modules = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                           else [alias.name for alias in node.names] if isinstance(node, ast.Import)
                           else [])
                if any(name == "edition_ee" or name.startswith("edition_ee.") for name in modules):
                    violations.append((str(path.relative_to(ROOT)), node.lineno))
    assert violations == []
