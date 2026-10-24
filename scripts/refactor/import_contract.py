"""Import-linter contract for the core migration's dependency ratchet."""
from __future__ import annotations

import ast
import json
from pathlib import Path

from importlinter.application import output
from importlinter.domain.contract import Contract, ContractCheck

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "internal design docs"
LAYERS = (
    ("core.agent_runtime",),
    ("core.runs", "core.jobs", "core.chat", "core.sandbox.commands"),
    ("core.llm",),
    ("core.evolution", "core.ontology"),
    ("core.memory", "core.sandbox", "core.capabilities", "core.files",
     "core.projects", "core.auth"),
    ("core.models",),
    ("core.db",),
    ("core.config", "core.infra"),
)
FEATURE_EDGES = {
    ("desktop", "marketplace"), ("desktop", "knowledge"),
    ("sites", "desktop"), ("workbench", "desktop"), ("hosted_apps", "desktop"),
    ("workbench", "sites"), ("hosted_apps", "sites"),
    ("automation", "desktop"),  # Authorized local task dispatch; see module README.
    ("distillation", "marketplace"),  # Explicit draft publication; see module README.
    ("security_console", "sandbox_ops"),  # Read-only rebuild status; see module README.
}
OUTER = ("features", "integrations", "personal_agent", "orchestration", "api")


def module_name(path):
    name = path.removesuffix(".py").replace("/", ".")
    return name.removesuffix(".__init__")


def belongs(name, prefix):
    return name == prefix or name.startswith(prefix + ".")


def canonical_names():
    # Apply the accepted manifest virtually before moves. Thus the baseline
    # already exposes dependency debt concealed by the old services directory.
    result = {}
    rows = json.loads((DATA / "manifest.json").read_text())
    supplemental = DATA / "supplemental_moves.json"
    if supplemental.exists():
        rows.extend(json.loads(supplemental.read_text()))
    for row in rows:
        if row["action"] == "move":
            result[module_name(row["src"])] = module_name(row["dst"])
    routes = DATA / "routes.json"
    if routes.exists():
        for row in json.loads(routes.read_text())["moves"]:
            result[module_name(row["src"])] = module_name(row["dst"])
    # A completed move may reuse an earlier source name (D11: capabilities).
    # Live destination modules represent their new owner, never the historical
    # alias. Preserve virtual classification only for paths not yet migrated.
    destinations = {module_name(row["dst"]) for row in rows if row["action"] == "move"}
    for name in destinations:
        module_path = ROOT / "src/backend" / Path(*name.split("."))
        if module_path.with_suffix(".py").is_file() or (module_path / "__init__.py").is_file():
            result.pop(name, None)
    test_moves = DATA / "test_moves.json"
    if test_moves.exists():
        from scripts.refactor.check_test_baseline import canonical_failures
        moves = json.loads(test_moves.read_text())
        canonical_failures([], moves)  # Validate one-to-one test-file migrations.
        for old, new in moves.items():
            result[module_name(new.removeprefix("src/backend/"))] = module_name(old.removeprefix("src/backend/"))
    return result


def layer(name):
    candidates = [
        (len(prefix), index)
        for index, prefixes in enumerate(LAYERS)
        for prefix in prefixes if belongs(name, prefix)
    ]
    return max(candidates)[1] if candidates else None


def edge_rule(source, target):
    if belongs(source, "core") and any(belongs(target, p) for p in OUTER):
        return "core-outward"
    if source.startswith("features.") and target.startswith("features."):
        a, b = source.split(".")[1], target.split(".")[1]
        if a != b and (a, b) not in FEATURE_EDGES:
            return "feature-direction"
    a, b = layer(source), layer(target)
    if a is not None and b is not None and a > b:
        return "core-layers"
    return None


def canonical(name, aliases):
    candidates = [key for key in aliases if belongs(name, key)]
    if not candidates:
        return name
    prefix = max(candidates, key=len)
    # A destination nested under its former module is already canonical.
    if any(belongs(name, target) and len(target) >= len(prefix) for target in aliases.values()):
        return name
    return aliases[prefix] + name[len(prefix):]


def private_imports_in_module(relative, text, aliases):
    physical_source = module_name(relative)
    source = canonical(physical_source, aliases)
    if belongs(source, "core"):
        return set()
    package = physical_source if relative.endswith("/__init__.py") else physical_source.rpartition(".")[0]
    result, bindings = set(), {}
    tree = ast.parse(text)

    def bind(name, target):
        # Keep every candidate across scopes: a later local alias must never
        # erase an outer import and hide a boundary violation.
        bindings.setdefault(name, set()).add(target)

    def record(target, symbol):
        target = canonical(target, aliases)
        if belongs(target, "core") and symbol.startswith("_"):
            result.add(f"private-symbol: {source} -> {target}.{symbol}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bind(alias.asname or alias.name.split(".")[0], alias.name if alias.asname else alias.name.split(".")[0])
        if not isinstance(node, ast.ImportFrom):
            continue
        target = node.module or ""
        if node.level:
            parts = package.split(".")
            target = ".".join(parts[:len(parts) - node.level + 1] + ([target] if target else []))
        for alias in node.names:
            record(target, alias.name)
            bind(alias.asname or alias.name, target + "." + alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
            continue
        parts, value = [], node.value
        while isinstance(value, ast.Attribute):
            parts.insert(0, value.attr)
            value = value.value
        if isinstance(value, ast.Name) and value.id in bindings:
            for target in bindings[value.id]:
                record(".".join([target, *parts]), node.attr)
    return result


def private_imports():
    result = set()
    backend = ROOT / "src/backend"
    aliases = canonical_names()
    for path in backend.rglob("*.py"):
        relative = path.relative_to(backend).as_posix()
        result.update(private_imports_in_module(relative, path.read_text(encoding="utf-8-sig"), aliases))
    return result


def package_marker_errors(backend):
    """Keep namespace directories from silently disappearing from grimp's graph."""
    missing = set()
    for root_name in ("core", "features", "integrations"):
        root = backend / root_name
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            parent = path.parent
            while parent != backend:
                if not (parent / "__init__.py").is_file():
                    missing.add("package-marker: " + parent.relative_to(backend).as_posix())
                parent = parent.parent
    return missing


def violations(graph):
    aliases = canonical_names()
    result = private_imports() | package_marker_errors(ROOT / "src/backend")
    for source in sorted(graph.modules):
        for target in graph.find_modules_directly_imported_by(source):
            rule = edge_rule(aliases.get(source, source), aliases.get(target, target))
            if rule:
                result.add(f"{rule}: {aliases.get(source, source)} -> {aliases.get(target, target)}")
    return result


class CoreBoundaryContract(Contract):
    """Require existing architecture violations to decrease, never increase."""

    def check(self, graph, verbose):
        current = violations(graph)
        baseline = set(json.loads((DATA / "import-baseline.json").read_text()))
        introduced = sorted(current - baseline)
        removed = sorted(baseline - current)
        return ContractCheck(
            kept=not introduced,
            metadata={"introduced": introduced},
            ignored_import_count=len(current & baseline),
            warnings=[f"Remove {len(removed)} resolved baseline entries"] if removed else [],
        )

    def render_broken_contract(self, check):
        for violation in check.metadata["introduced"]:
            output.print_error(violation)
