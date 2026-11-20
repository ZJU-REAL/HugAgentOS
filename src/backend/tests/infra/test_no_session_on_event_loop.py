"""Static guard: async code never touches a database session.

Database work belongs in a sync function run through ``core.db.threads``
(``run_in_session`` / ``run_db`` / ``@off_loop``); see that module for the rule.
An async function — route, dependency, tool, worker or helper — therefore never
opens a session, never binds one, and never receives one. There are no
exceptions: a new site fails this test instead of joining a list.

The literal check misses the indirect forms: an async function calling a sync
helper that opens its own session, or binding a repository to the session
factory and calling it in place (``Repo(SessionLocal).method()``). The second
guard catches both, one call deep, in the packages listed in
``INDIRECT_SCOPE``; widen that list as other packages are cleaned up.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]

# Packages whose async code is also checked for indirect session use.
INDIRECT_SCOPE = (
    "core/agent_runtime/",
    "core/chat/",
    "core/harness/",
    "core/llm/",
    "core/runs/",
    "orchestration/",
    "prompts/",
)

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


def _backend_modules():
    """``{module name: (relative path, tree)}`` for production backend code."""
    modules = {}
    for path in sorted(BACKEND.rglob("*.py")):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith(("tests/", "alembic/")) or "/node_modules/" in relative:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        name = relative[: -len(".py")].replace("/", ".").removesuffix(".__init__")
        modules[name] = (relative, tree)
    return modules


def _opens_session(function):
    return any(
        isinstance(node, ast.Call) and _name(node.func).endswith("SessionLocal")
        for node in _own_nodes(function)
    )


def _imports(tree, modules):
    """Local name → ``("module", name)`` or ``("function", module, name)``."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    found[alias.asname] = ("module", alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                local = alias.asname or alias.name
                found[local] = (
                    ("module", full) if full in modules else ("function", node.module, alias.name)
                )
    return found


def _callee(func, module, imports, helpers):
    """The ``(module, function)`` a call resolves to, when it is a known helper."""
    if isinstance(func, ast.Name):
        if (module, func.id) in helpers:
            return module, func.id
        target = imports.get(func.id)
        if target and target[0] == "function":
            return target[1], target[2]
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        target = imports.get(func.value.id)
        if target and target[0] == "module":
            return target[1], func.attr
    return None


def _bound_to_factory(call):
    """``Repo(SessionLocal).method(...)``: a repository run in place on the loop."""
    owner = call.func.value if isinstance(call.func, ast.Attribute) else None
    return isinstance(owner, ast.Call) and any(
        _name(arg).endswith("SessionLocal") for arg in owner.args
    )


def _indirect_violations(modules, scope):
    helpers = {
        (module, node.name)
        for module, (_, tree) in modules.items()
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and _opens_session(node)
    }
    found = []
    for module, (relative, tree) in modules.items():
        if not relative.startswith(scope):
            continue
        imports = _imports(tree, modules)
        for function in ast.walk(tree):
            if not isinstance(function, ast.AsyncFunctionDef):
                continue
            for node in _own_nodes(function):
                if not isinstance(node, ast.Call):
                    continue
                callee = _callee(node.func, module, imports, helpers)
                if callee in helpers:
                    found.append(
                        f"{relative}:{node.lineno} {function.name} calls {'.'.join(callee)}"
                    )
                elif _bound_to_factory(node):
                    found.append(
                        f"{relative}:{node.lineno} {function.name} runs a SessionLocal repository"
                    )
    return found


def test_async_code_never_touches_a_session():
    offenders = []
    for relative, tree in _backend_modules().values():
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


def test_async_runtime_never_calls_a_session_helper():
    offenders = _indirect_violations(_backend_modules(), INDIRECT_SCOPE)
    assert offenders == [], "await the helper through core.db.threads.run_db:\n" + "\n".join(
        offenders
    )


def test_indirect_guard_recognises_each_pattern():
    helpers = ast.parse(
        """
from core.db.engine import SessionLocal
def load(run_id):
    with SessionLocal() as db:
        return db.get(Run, run_id)
def deferred():
    def inner():
        return SessionLocal()
    return inner
"""
    )
    caller = ast.parse(
        """
import pkg.helpers as helpers
from pkg.helpers import load
from core.db.engine import SessionLocal
async def direct(run_id):
    return load(run_id)
async def through_module(run_id):
    return helpers.load(run_id)
async def bound(run_id):
    return Queue(SessionLocal).claim(run_id)
async def offloaded(run_id):
    return await run_db(load, run_id)
async def nested_only():
    return helpers.deferred()
async def passed_along():
    return await recover(journal=Journal(SessionLocal))
"""
    )
    modules = {"pkg.helpers": ("pkg/helpers.py", helpers), "pkg.caller": ("pkg/caller.py", caller)}
    found = _indirect_violations(modules, ("pkg/caller.py",))
    assert sorted(item.split()[1] for item in found) == ["bound", "direct", "through_module"]
