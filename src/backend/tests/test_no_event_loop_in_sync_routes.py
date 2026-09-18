"""同步路由不得伸手去要事件循环。

这是 ``test_no_blocking_async_routes`` 的反向守卫，两条合起来才完整：那条要求「拿同步
会话的路由必须是 ``def``」，于是路由体从此跑在 FastAPI 的线程池里，**没有事件循环**。
任何在那里 ``asyncio.create_task(...)`` / ``get_running_loop()`` 的代码都会抛
``RuntimeError: no running event loop``——而且只在运行时抛，代码看着完全正常。

历史教训：把 455 个路由批量改成 ``def`` 的那次提交，顺手废掉了「立即执行」和「发起技能
蒸馏」两个功能，两者都在生产上 100% 失败了很久没人发现——失败长得像业务失败，不像派发
没成功。

要跨请求存活的工作请落库交给队列工人（``orchestration/schedulers/_worker_base``）；
确实在事件循环上的临时任务用 ``core.infra.background.spawn``。
"""

import ast
import pathlib

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
LOOP_CALLS = {
    "create_task",
    "ensure_future",
    "get_event_loop",
    "get_running_loop",
    "run_coroutine_threadsafe",
}
BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCANNED_PACKAGES = ("api", "edition_ee")
SKIP_DIRS = {"__pycache__", "tests", "alembic"}


def _is_route(node: ast.FunctionDef) -> bool:
    return any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and d.func.attr in HTTP_METHODS
        for d in node.decorator_list
    )


def _loop_reaches(node: ast.AST) -> list:
    """``asyncio.<loop-call>(...)`` 的调用点。

    只认 ``asyncio.`` 前缀，``self._task.cancel()`` 之类的同名方法不算。
    """
    hits = []
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr in LOOP_CALLS
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "asyncio"
        ):
            hits.append("asyncio.%s @ line %d" % (sub.func.attr, sub.lineno))
    return hits


def _index_sync_functions():
    """全后端的同步函数索引，用来跟一层调用——路由自己干净、被它调用的服务不干净是常态。"""
    index = {}
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                index.setdefault(node.name, []).append((str(path.relative_to(BACKEND_ROOT)), node))
    return index


def _called_names(node: ast.AST):
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                names.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                names.add(sub.func.attr)
    return names


def _offenders():
    sync_functions = _index_sync_functions()
    found = []
    for package in SCANNED_PACKAGES:
        root = BACKEND_ROOT / package
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # ast.FunctionDef（而非 AsyncFunctionDef）即同步路由
                if not isinstance(node, ast.FunctionDef) or not _is_route(node):
                    continue
                where = "%s:%d %s()" % (path.relative_to(BACKEND_ROOT), node.lineno, node.name)
                direct = _loop_reaches(node)
                if direct:
                    found.append("%s -> %s" % (where, ", ".join(direct)))
                    continue
                for callee in sorted(_called_names(node)):
                    hit = None
                    for callee_path, callee_node in sync_functions.get(callee, []):
                        reaches = _loop_reaches(callee_node)
                        if reaches:
                            hit = "%s -> %s() @ %s -> %s" % (
                                where,
                                callee,
                                callee_path,
                                ", ".join(reaches),
                            )
                            break
                    if hit:
                        found.append(hit)
                        break
    return found


def test_sync_routes_do_not_reach_for_the_event_loop():
    offenders = _offenders()
    assert not offenders, (
        "以下同步路由（直接或经它调用的同步函数）伸手去要事件循环。同步路由跑在 FastAPI "
        "的线程池里，那里没有循环，运行时必抛 RuntimeError：\n  " + "\n  ".join(offenders)
    )
