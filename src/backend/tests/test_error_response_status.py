"""Guard: ``error_response`` defaults to HTTP 500, so a call that passes an
HTTP-style business code (e.g. ``code=404``) must also pass ``status_code``.
Otherwise "not found" / "access denied" responses reach clients as 500."""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"tests", "alembic", "__pycache__"}


def _calls_missing_status(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8", errors="replace")
    if "error_response(" not in source:
        return []
    tree = ast.parse(source, filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "error_response":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        if "status_code" in kwargs or len(node.args) >= 4:
            continue
        code = kwargs.get("code", node.args[0] if node.args else None)
        if isinstance(code, ast.Constant) and isinstance(code.value, int) and 400 <= code.value <= 599:
            found.append(f"{path.relative_to(BACKEND)}:{node.lineno} code={code.value}")
    return found


def test_http_style_error_codes_set_status_code():
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if SKIP_DIRS.intersection(path.relative_to(BACKEND).parts):
            continue
        offenders.extend(_calls_missing_status(path))
    assert not offenders, "error_response calls fall back to HTTP 500:\n" + "\n".join(offenders)
