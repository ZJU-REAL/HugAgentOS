"""Governed tool registry and resource-intent declarations."""

from __future__ import annotations
import os
import logging
from typing import Sequence, Any, Mapping, Optional
from core.sandbox.permissions.dispatch_models import PermissionRuntime, PermissionIntent, ToolPermissionSpec, DOMAIN_DENY, DOMAIN_MYSPACE, DOMAIN_LOCAL_PATH, DOMAIN_LOCAL_COMMAND, READ, WRITE, EXECUTE, _canonical_path

logger = logging.getLogger(__name__)


class ToolPermissionRegistry:
    """Mutable run-scoped governed-tool list; plugins extend the same object."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolPermissionSpec] = {}
        self._sources: dict[str, str] = {}

    def register(
        self,
        tool_name: str,
        spec: ToolPermissionSpec,
        *,
        source: str,
    ) -> None:
        name = str(tool_name or "").strip()
        if not name:
            raise ValueError("tool permission name must not be empty")
        previous = self._specs.get(name)
        if previous is not None and previous.key != spec.key:
            raise ValueError(
                "conflicting permission declarations for tool "
                f"{name!r}: {previous.key!r} from {self._sources[name]!r} "
                f"vs {spec.key!r} from {source!r}"
            )
        self._specs[name] = spec
        self._sources[name] = source

    def get(self, tool_name: str) -> Optional[ToolPermissionSpec]:
        return self._specs.get(tool_name)

    def contains(self, tool_name: str) -> bool:
        return tool_name in self._specs

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._specs)


def allow_tool() -> ToolPermissionSpec:
    return ToolPermissionSpec("allow", lambda _args, _runtime: ())


def deny_tool(reason: str) -> ToolPermissionSpec:
    def resolve(_args: Mapping[str, Any], _runtime: PermissionRuntime):
        return (
            PermissionIntent(
                domain=DOMAIN_DENY,
                action="deny",
                target="",
                summary=reason,
            ),
        )

    return ToolPermissionSpec(f"deny:{reason}", resolve)


def _is_myspace_target(logical: str, physical: str, user_id: Optional[str]) -> bool:
    """Whether this access lands in the user's persistent My Space area.

    Judged on the physical mount path used for file access.
    Testing the raw argument for a ``/myspace`` prefix
    would miss the equally-supported physical spelling
    ``/workspace/myspace/<uid>/…``, letting a caller reach persistent storage
    with no confirmation at all.
    """
    if logical == "/myspace" or logical.startswith("/myspace/"):
        return True
    from core.sandbox.workspace.resolution import is_myspace_physical

    return bool(is_myspace_physical(physical, user_id))


def _path_summary(tool: str, action: str, logical: str, args: Mapping[str, Any]) -> str:
    if tool == "Write":
        return f"写入 {logical}（{len(str(args.get('content') or ''))} 字符）"
    if tool == "Edit":
        return f"编辑 {logical}（替换片段）"
    labels = {
        "Read": "读取",
        "Glob": "扫描",
        "Grep": "搜索",
        "read_image": "查看图片",
        "sandbox_put_artifact": "写入沙盒文件",
        "sandbox_get_artifact": "读取沙盒文件",
    }
    return f"{labels.get(tool, action)} {logical}"


def local_path_tool(
    path_arg: str,
    action: str,
    *,
    tool_name: str = "",
    additional_paths: Sequence[tuple[str, str]] = (),
    myspace_op: str = "",
    skip_if_arg: str = "",
    default_path: str = "",
) -> ToolPermissionSpec:
    """Declare a tool whose effects are host paths taken from its arguments.

    ``default_path`` mirrors the tool signature's own default, so a call that
    omits the optional argument is still governed instead of resolving to zero
    intents and passing through ungoverned.
    """
    path_specs = ((path_arg, action), *tuple(additional_paths))
    key = "path:" + ",".join(f"{arg}:{mode}" for arg, mode in path_specs)
    if myspace_op:
        key += f":myspace:{myspace_op}"
    if default_path:
        key += f":default:{default_path}"

    def resolve(args: Mapping[str, Any], runtime: PermissionRuntime):
        if skip_if_arg and str(args.get(skip_if_arg) or "").strip():
            return ()
        intents: list[PermissionIntent] = []
        first: Optional[tuple[str, str]] = None
        from core.sandbox.workspace.resolution import to_physical_path

        for index, (arg, mode) in enumerate(path_specs):
            logical = str(args.get(arg) or "").strip()
            if not logical and index == 0:
                logical = default_path
            if not logical:
                continue
            session = runtime.sandbox_session_id or runtime.chat_id
            physical = to_physical_path(
                logical, runtime.user_id, session_id=session, scope=runtime.project_scope
            )
            if first is None:
                first = (logical, physical)
            intents.append(
                PermissionIntent(
                    domain=DOMAIN_LOCAL_PATH,
                    action=mode,
                    target=physical,
                    summary=_path_summary(tool_name, mode, logical, args),
                )
            )
        if myspace_op and first is not None and _is_myspace_target(*first, runtime.user_id):
            intents.append(
                PermissionIntent(
                    domain=DOMAIN_MYSPACE,
                    action=WRITE,
                    target=first[0],
                    summary=_path_summary(tool_name, WRITE, first[0], args),
                    op=myspace_op,
                    kind="myspace",
                )
            )
        return tuple(intents)

    return ToolPermissionSpec(key, resolve)


def local_command_tool() -> ToolPermissionSpec:
    def resolve(args: Mapping[str, Any], _runtime: PermissionRuntime):
        command = str(args.get("command") or "").strip()
        if not command:
            return ()
        return (
            PermissionIntent(
                domain=DOMAIN_LOCAL_COMMAND,
                action=EXECUTE,
                target=command,
                summary=f"在本机执行：{command[:200]}",
                op="local_exec",
                kind="local_cmd",
            ),
        )

    return ToolPermissionSpec("local-command:command", resolve)


def builtin_tool_permission(tool_name: str) -> Optional[ToolPermissionSpec]:
    """Return the rule for a governed native tool, otherwise pass through."""
    from core.sandbox.permissions.vocabulary import OP_EDIT, OP_WRITE

    governed: dict[str, ToolPermissionSpec] = {
        "Read": local_path_tool("file_path", READ, tool_name="Read"),
        "Write": local_path_tool("file_path", WRITE, tool_name="Write", myspace_op=OP_WRITE),
        "Edit": local_path_tool("file_path", WRITE, tool_name="Edit", myspace_op=OP_EDIT),
        # ``path`` is optional on both; mirror their signature default so an
        # omitted argument is still governed rather than resolving to no intent.
        "Glob": local_path_tool("path", READ, tool_name="Glob", default_path="/workspace"),
        "Grep": local_path_tool("path", READ, tool_name="Grep", default_path="/workspace"),
        "read_image": local_path_tool(
            "file_path",
            READ,
            tool_name="read_image",
            skip_if_arg="file_id",
        ),
        "Bash": local_command_tool(),
        "sandbox_put_artifact": local_path_tool(
            "dest_path", WRITE, tool_name="sandbox_put_artifact"
        ),
        "sandbox_get_artifact": local_path_tool("src_path", READ, tool_name="sandbox_get_artifact"),
        # 批量作业会把 ``script_path`` 的内容读出来当脚本正文执行，``dest_path`` 是
        # 它导出台账的落点。两者都是模型给的裸路径，必须和其它文件工具同样受管，
        # 否则这条路等于一个不受策略约束的任意路径读取通道。
        "run_job": local_path_tool(
            "script_path",
            READ,
            tool_name="run_job",
            additional_paths=(("dest_path", WRITE),),
        ),
    }
    return governed.get(tool_name)


def _too_broad_write_root(path: str) -> bool:
    """Whether granting ``path`` as a writable root would hand out far too much.

    A not-yet-created target has to be widened to its parent directory for the
    file to be creatable at all. That is fine for ``~/project/out.txt`` and very
    much not fine for ``~/out.txt``: the home directory holds every credential
    file and dotfile the user owns. Compare by resolved identity so a symlinked
    home is recognised too.
    """
    resolved = _canonical_path(path)
    if resolved == os.path.dirname(resolved):  # filesystem / drive root
        return True
    try:
        home = _canonical_path(os.path.expanduser("~"))
    except (OSError, RuntimeError):
        return False
    return resolved == home


def _one_shot_write_root(path: str) -> Optional[str]:
    target = _canonical_path(path)
    if os.path.exists(target):
        return target
    parent = os.path.dirname(target)
    if parent == target or not os.path.isdir(parent):
        return None
    if _too_broad_write_root(parent):
        logger.info("[tool-permission] refusing over-broad one-shot write root %r", parent)
        return None
    return parent
