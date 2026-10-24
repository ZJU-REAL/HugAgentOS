"""Static guard: async code never touches a database session.

Database work belongs in a sync function run through ``core.db.threads``
(``run_in_session`` / ``run_db`` / ``@off_loop``); see that module for the rule.
An async function — route, dependency, tool, worker or helper — therefore never
opens a session, never binds one, and never receives one. There are no
exceptions: a new site fails this test instead of joining a list.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]

# Names an open session is bound to (``with factory() as db``), whatever the factory.
SESSION_NAMES = {"db", "session_db", "cleanup_db"}


def _name(node):
    return getattr(node, "id", None) or getattr(node, "attr", "") or ""


def _own_nodes(function):
    """Nodes of ``function``'s body, excluding nested functions (those run elsewhere)."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _session_parameters(function):
    args = function.args
    positional = args.posonlyargs + args.args
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for arg, default in zip(positional + args.kwonlyargs, defaults + list(args.kw_defaults)):
        injected = (
            isinstance(default, ast.Call)
            and _name(default.func) == "Depends"
            and bool(default.args)
            and _name(default.args[0]) == "get_db"
        )
        annotated = arg.annotation is not None and _name(arg.annotation) == "Session"
        if injected or annotated or arg.arg in SESSION_NAMES:
            yield arg.arg


def _violations(function):
    found = [f"parameter {name}" for name in _session_parameters(function)]
    for node in _own_nodes(function):
        if isinstance(node, ast.Call) and _name(node.func).endswith("SessionLocal"):
            found.append(f"line {node.lineno}: opens a session")
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if _name(item.optional_vars) in SESSION_NAMES:
                    found.append(f"line {node.lineno}: binds a session")
    return found


def test_async_code_never_touches_a_session():
    offenders = []
    for path in sorted(BACKEND.rglob("*.py")):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith(("tests/", "alembic/")) or "/node_modules/" in relative:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for function in ast.walk(tree):
            if isinstance(function, ast.AsyncFunctionDef):
                offenders += [
                    f"{relative}:{function.name} ({why})" for why in _violations(function)
                ]
    assert offenders == [], "move the database step into core.db.threads.run_in_session"


def test_guard_recognises_each_pattern():
    source = '''
async def injected(db: Session = Depends(get_db)): ...
async def annotated(conn: Session): ...
async def opened():
    s = SessionLocal()
async def bound(factory):
    with factory() as db:
        pass
async def clean(item_id):
    return await run_in_session(_load, item_id)
'''
    functions = {
        node.name: node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.AsyncFunctionDef)
    }
    assert {name for name, fn in functions.items() if _violations(fn)} == {
        "injected",
        "annotated",
        "opened",
        "bound",
    }
