"""三个沙箱实现必须都认 ``/myspace``——模型只被告知这一种写法。

opensandbox / cube 是一人一沙箱，靠容器里的软链；script_runner 是多用户共享的服务，
不能建全局软链（会指向最后一个用的人），改成按请求把路径改写到该用户目录。
"""

import sys
from pathlib import Path

import pytest

_SERVICES = Path(__file__).resolve().parents[2] / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))

UID = "user_a9e0c627c2674456"


# ── script_runner：按请求改写，不留全局软链 ──────────────────────────────────


def _rewrite(value, user_id=UID):
    from script_runner_service.server import _rewrite_myspace_refs

    return _rewrite_myspace_refs(value, user_id)


def test_myspace_expands_to_the_requesting_users_directory():
    assert _rewrite("cat /myspace/报告/x.txt") == f"cat /workspace/myspace/{UID}/报告/x.txt"
    assert _rewrite("ls /myspace") == f"ls /workspace/myspace/{UID}"


def test_quoting_survives_the_rewrite():
    assert _rewrite("cp a '/myspace/b c.txt'") == f"cp a '/workspace/myspace/{UID}/b c.txt'"


def test_without_a_user_the_path_is_left_alone():
    """无从判断是谁的空间时宁可让路径不存在而报错，绝不能猜一个用户。"""
    assert _rewrite("cat /myspace/x", user_id=None) == "cat /myspace/x"


def test_lookalike_paths_are_not_touched():
    """边界必须精确：/myspaces 和 /a/myspace 都不是那个入口。"""
    assert _rewrite("cat /myspaces/x /a/myspace/y") == "cat /myspaces/x /a/myspace/y"


def test_rewrite_rejects_a_traversal_user_id():
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _rewrite("cat /myspace/x", user_id="../other")


def test_bash_path_lands_in_the_session_workspace():
    from script_runner_service.server import _rewrite_execution_paths

    out = _rewrite_execution_paths(
        "cat /myspace/x.txt", "bash", "/workspace/session_workspaces/abc", user_id=UID
    )
    assert out == f"cat /workspace/session_workspaces/abc/myspace/{UID}/x.txt"


# ── cube：软链拼进执行命令，不额外多花一次往返 ──────────────────────────────


def test_cube_command_creates_the_symlink_inline():
    """cube 一人一沙箱，可以建软链；必须拼在同一条命令里，且不影响脚本退出码。"""
    import inspect

    from core.sandbox.cube_provider import CubeSandboxProvider

    src = inspect.getsource(CubeSandboxProvider._run)
    assert "ln -s {mp} /myspace" in src, "cube 必须建 /myspace 软链"
    assert "{myspace_prefix}cd {WORKSPACE}" in src, "软链要拼进同一条命令，不另花一次往返"
