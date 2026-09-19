"""路由不得把阻塞式数据库调用压在事件循环上。

同步 SQLAlchemy Session 的每一次查询都是阻塞调用。路由声明成 ``async def`` 时它直接跑在
事件循环上，一次行锁等待就能冻结整个进程：等锁的请求占住事件循环，持锁的请求又要拿回
事件循环才能收尾释放锁，两边互相干等，全部接口一起停摆（生产曾因站点面板并发只读请求
触发过一次）。声明成 ``def`` 则由 FastAPI 丢进线程池，阻塞只占一个线程。

因此：用同步会话（``db: Session`` 参数或就地 ``SessionLocal()``）且函数体内没有任何
``await`` 的路由，必须写成 ``def``。
"""

import ast
import pathlib

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCANNED_PACKAGES = ("api", "edition_ee")


def _is_route(node: ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and d.func.attr in HTTP_METHODS
        for d in node.decorator_list
    )


def _uses_sync_session(node: ast.AsyncFunctionDef) -> bool:
    """声明式（``db: Session``）与就地开的（``with SessionLocal()``）一样算。

    只认参数注解会漏掉后者，而后者一样在事件循环上跑阻塞查询——生产上请求量最大的
    ``/v1/jobs`` 正是这样从这条守卫底下溜过去的。
    """
    if any(
        arg.annotation is not None and ast.unparse(arg.annotation) == "Session"
        for arg in list(node.args.args) + list(node.args.kwonlyargs)
    ):
        return True
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Name)
        and sub.func.id == "SessionLocal"
        for sub in ast.walk(node)
    )


def _has_await(node: ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(sub, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for sub in ast.walk(node)
    )


def _offenders():
    found = []
    for package in SCANNED_PACKAGES:
        root = BACKEND_ROOT / package
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.AsyncFunctionDef)
                    and _is_route(node)
                    and _uses_sync_session(node)
                    and not _has_await(node)
                ):
                    found.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno} {node.name}")
    return found


def test_sync_db_routes_are_not_declared_async():
    offenders = _offenders()
    assert not offenders, (
        "以下路由拿同步数据库会话却声明为 async def，且体内没有任何 await——"
        "它们会把阻塞查询压在事件循环上，一次锁等待即可冻结整个后端。"
        "去掉 async 即可（FastAPI 会把 def 路由放进线程池）：\n  "
        + "\n  ".join(offenders)
    )
