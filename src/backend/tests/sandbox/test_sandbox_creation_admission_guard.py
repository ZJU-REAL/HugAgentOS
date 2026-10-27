"""Every OpenSandbox creation must pass through capacity admission.

A direct ``Sandbox.create`` would bypass the shared ledger and let the
deployment over-admit. The only accepted shape is
``<admission>.create(lambda metadata: Sandbox.create(..., metadata=metadata))``.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
SKIP = {"tests", "__pycache__", "node_modules"}


def _is_sandbox_create(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "Sandbox")


def _admitted(lambda_node: ast.Lambda, parents: dict) -> bool:
    call = parents.get(lambda_node)
    return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "create" and call.args and call.args[0] is lambda_node)


def _passes_admitted_metadata(node: ast.Call, lambda_node: ast.Lambda) -> bool:
    param = lambda_node.args.args[0].arg if lambda_node.args.args else None
    return any(kw.arg == "metadata" and isinstance(kw.value, ast.Name) and kw.value.id == param
               for kw in node.keywords)


def test_no_sandbox_create_bypasses_admission():
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if SKIP & set(path.relative_to(BACKEND).parts):
            continue
        source = path.read_text(encoding="utf-8")
        if "Sandbox.create" not in source:
            continue
        tree = ast.parse(source)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not _is_sandbox_create(node):
                continue
            owner = parents.get(node)
            while owner is not None and not isinstance(owner, ast.Lambda):
                owner = parents.get(owner)
            if (owner is None or not _admitted(owner, parents)
                    or not _passes_admitted_metadata(node, owner)):
                offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
    assert not offenders, "Sandbox.create outside admission: " + ", ".join(offenders)
