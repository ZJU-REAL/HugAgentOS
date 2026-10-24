"""AST-based import rewrite planner. Preview by default; --apply writes changes."""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "internal design docs"


def module_name(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def mapped(value: str, moves: dict[str, str]) -> str:
    for old in sorted(moves, key=len, reverse=True):
        if value == old or value.startswith(old + "."):
            return moves[old] + value[len(old):]
    return value


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        if prefix:
            return prefix + "." + node.attr
    return None


def rewrite(source, old_module, moves, symbols=None, *, package=False):
    """Return source and unresolved string references; do not execute imports."""
    symbols = symbols or {}
    tree = ast.parse(source)
    # AST offsets are UTF-8 bytes, not character positions.
    lines = source.encode("utf-8").splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    patches = []
    unresolved = []
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    imported_names = {}
    shadowed_names = set()
    wildcard_import = False

    def record_import(binding, target):
        previous = imported_names.get(binding)
        if previous is not None and previous != target:
            shadowed_names.add(binding)
        imported_names[binding] = target

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                record_import(
                    alias.asname or alias.name.split(".")[0],
                    alias.name if alias.asname else alias.name.split(".")[0],
                )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    wildcard_import = True
                record_import(alias.asname or alias.name, "." * node.level + (node.module or "") + "." + alias.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            shadowed_names.add(node.id)
        elif isinstance(node, ast.arg):
            shadowed_names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            shadowed_names.add(node.name)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            shadowed_names.add(node.name)
        elif isinstance(node, ast.MatchMapping):
            shadowed_names.add(node.rest)

    def patch_target(node):
        parent = parents.get(node)
        call = parents.get(parent) if isinstance(parent, ast.keyword) else parent
        if not isinstance(call, ast.Call):
            return False
        first_argument = bool(call.args) and call.args[0] is node
        target_keyword = isinstance(parent, ast.keyword) and parent.arg == "target"
        if not (first_argument or target_keyword):
            return False
        name = dotted(call.func) or ""
        head, _, suffix = name.partition(".")
        resolved = imported_names.get(head, head) + ("." + suffix if suffix else "")
        if not wildcard_import and head not in shadowed_names and resolved in {
            "unittest.mock.patch", "unittest.mock.patch.dict", "unittest.mock.patch.multiple"
        }:
            return True
        if name != "monkeypatch.setattr":
            return False
        ancestor = call
        while ancestor in parents:
            ancestor = parents[ancestor]
            if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = ancestor.args.posonlyargs + ancestor.args.args + ancestor.args.kwonlyargs
                if not any(arg.arg == "monkeypatch" for arg in args):
                    continue
                rebound = any(
                    (isinstance(child, ast.Name) and child.id == "monkeypatch"
                     and isinstance(child.ctx, (ast.Store, ast.Del)))
                    or (isinstance(child, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar))
                        and child.name == "monkeypatch")
                    or (isinstance(child, ast.MatchMapping) and child.rest == "monkeypatch")
                    or (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and child.name == "monkeypatch")
                    or (isinstance(child, ast.alias)
                        and (child.asname or child.name.split(".")[0]) in {"monkeypatch", "*"})
                    for child in ast.walk(ancestor)
                )
                return not rebound
        return False

    def replace(node, text):
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        patches.append((start, end, text.encode("utf-8")))

    def import_lines(node, statements):
        prefix = lines[node.lineno - 1][:node.col_offset].decode("utf-8")
        separator = "; " if prefix.strip() else "\n" + prefix
        replacement = separator.join(statements)
        original = ast.get_source_segment(source, node)
        if replacement != original:
            replace(node, replacement)

    unaliased = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            statements = []
            for alias in node.names:
                target = mapped(alias.name, moves)
                statements.append("import " + target + (" as " + alias.asname if alias.asname else ""))
                if target != alias.name and not alias.asname:
                    unaliased[alias.name] = target
            # Only rewrite changed imports, preserving comments/format otherwise.
            if any(mapped(a.name, moves) != a.name for a in node.names):
                import_lines(node, statements)
        elif isinstance(node, ast.ImportFrom):
            original_module = node.module or ""
            if node.level:
                containing = old_module if package else old_module.rpartition(".")[0]
                original_module = importlib.util.resolve_name(
                    "." * node.level + original_module, containing
                )
            groups = {}
            for alias in node.names:
                submodule = original_module + "." + alias.name
                if submodule in moves:
                    dest = moves[submodule]
                    parent, _, name = dest.rpartition(".")
                    renamed = ast.alias(name=name, asname=alias.asname or (
                        alias.name if name != alias.name else None
                    ))
                    groups.setdefault(parent, []).append(renamed)
                else:
                    target = mapped(original_module, moves)
                    if original_module == "core.db.models" or original_module.startswith("core.db.models."):
                        target = symbols.get(alias.name, target)
                    groups.setdefault(target, []).append(alias)
            moved_self = old_module in moves
            changed = any(target != original_module for target in groups)
            changed |= any(original_module + "." + a.name in moves for a in node.names)
            if changed or (node.level and moved_self):
                statements = [
                    "from " + target + " import " + ", ".join(
                        a.name + (" as " + a.asname if a.asname else "") for a in aliases
                    )
                    for target, aliases in groups.items()
                ]
                import_lines(node, statements)

    protected_roots = {
        name.split(".")[0] for pair in unaliased.items() for name in pair
    }
    for node in ast.walk(tree):
        binding = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            binding = node.id
        elif isinstance(node, ast.arg):
            binding = node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            binding = node.name
        elif isinstance(node, ast.alias):
            parent = parents.get(node)
            if node.asname:
                binding = node.asname
            elif isinstance(parent, ast.ImportFrom):
                binding = node.name
        elif isinstance(node, ast.ExceptHandler):
            binding = node.name
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            binding = node.name
        elif isinstance(node, ast.MatchMapping):
            binding = node.rest
        if binding in protected_roots:
            raise ValueError(f"ambiguous binding {binding!r} at line {node.lineno}; manual rewrite required")

    changed_roots = {
        old.split(".")[0] for old, new in unaliased.items()
        if old.split(".")[0] != new.split(".")[0]
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
            continue
        if node.id not in changed_roots:
            continue
        usage = node
        while isinstance(parents.get(usage), ast.Attribute):
            usage = parents[usage]
        name = dotted(usage)
        if mapped(name, unaliased) == name:
            raise ValueError(f"unresolved root use {name!r} at line {node.lineno}; manual rewrite required")

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and not isinstance(parents.get(node), ast.Attribute):
            name = dotted(node)
            if name and mapped(name, unaliased) != name:
                replace(node, mapped(name, unaliased))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            target = node.value
            for old in sorted(moves, key=len, reverse=True):
                target = re.sub(
                    r"(?<![\w.])" + re.escape(old) + r"(?![\w])",
                    moves[old], target,
                )
            if target == node.value:
                continue
            if patch_target(node):
                replace(node, repr(target))
            else:
                unresolved.append({"line": node.lineno, "old": node.value, "new": target})
    result = source.encode("utf-8")
    end_of_previous = len(result) + 1
    for start, end, replacement in sorted(patches, reverse=True):
        if end > end_of_previous:
            raise ValueError("Overlapping AST edits; manual review required")
        result = result[:start] + replacement + result[end:]
        end_of_previous = start
    result = result.decode("utf-8")
    ast.parse(result)
    return result, unresolved


def load_moves(batch):
    rows = json.loads((DATA / "manifest.json").read_text())
    supplemental = DATA / "supplemental_moves.json"
    if supplemental.exists():
        for extra in json.loads(supplemental.read_text()):
            if extra.get("override") is not True:
                rows.append(extra)
                continue
            matches = [i for i, row in enumerate(rows) if row["src"] == extra["src"]]
            if (len(matches) != 1 or not extra.get("note", "").strip()
                    or rows[matches[0]]["batch"] != extra["batch"]
                    or rows[matches[0]]["action"] != extra["action"]):
                raise ValueError("Invalid reviewed migration override")
            rows[matches[0]] = extra
    routes = json.loads((DATA / "routes.json").read_text())["moves"]
    sources, destinations, destination_modules = set(), set(), set()
    for row in rows + routes:
        if row.get("action", "move") != "move":
            continue
        if row["src"] in sources or row["dst"] in destinations or module_name(row["dst"]) in destination_modules:
            raise ValueError(f"Conflicting migration path: {row['src']} -> {row['dst']}")
        sources.add(row["src"])
        destinations.add(row["dst"])
        destination_modules.add(module_name(row["dst"]))
    return {
        module_name(row["src"]): module_name(row["dst"])
        for row in rows + routes
        if row["batch"] == batch and row.get("action", "move") == "move"
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--symbols", type=Path, help="Reviewed symbol subset for this batch")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    moves = load_moves(args.batch)
    if not moves:
        parser.error("Batch has no pure file moves; provide a supported migration batch")
    if args.symbols:
        symbols = json.loads(args.symbols.read_text())
    else:
        owner = args.batch.split("-", 1)[-1]
        symbols = {
            name: target
            for name, target in json.loads((DATA / "symbol_moves.json").read_text()).items()
            if target in {f"features.{owner}.models", f"integrations.{owner}.models"}
        }
    reverse = {target: old for old, target in moves.items()}
    changes = []
    report = {
        "batch": args.batch, "changed_files": [], "unresolved_strings": [],
        "core_string_inventory": [], "non_runtime_resources": [],
    }
    for base in ("src/backend", "ce/overlay/src/backend", "evaluation"):
        directory = ROOT / base
        for path in sorted(directory.rglob("*.py")):
            relative = path.relative_to(directory).as_posix()
            module = module_name(relative)
            if base == "evaluation":
                module = "evaluation" if relative == "__init__.py" else "evaluation." + module
            old = reverse.get(module, module)
            source = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                if "core." in source or "from core" in source or old in moves:
                    raise ValueError(f"Cannot parse migration source: {path}") from None
                report["non_runtime_resources"].append(path.relative_to(ROOT).as_posix())
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "core." in node.value:
                    report["core_string_inventory"].append({
                        "file": path.relative_to(ROOT).as_posix(),
                        "line": node.lineno, "value": node.value,
                    })
            try:
                result, unresolved = rewrite(
                    source, old, moves, symbols, package=path.name == "__init__.py"
                )
            except ValueError as error:
                raise ValueError(f"{path.relative_to(ROOT)}: {error}") from error
            if result != source:
                changes.append((path, result))
                report["changed_files"].append(path.relative_to(ROOT).as_posix())
            for item in unresolved:
                report["unresolved_strings"].append({
                    "file": path.relative_to(ROOT).as_posix(), **item
                })
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.apply and report["unresolved_strings"]:
        print("No files written: review unresolved module strings in", args.report)
        return 2
    if args.apply:
        for path, result in changes:
            path.write_text(result)
    print(f"{len(changes)} files planned; {len(report['unresolved_strings'])} strings need review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
