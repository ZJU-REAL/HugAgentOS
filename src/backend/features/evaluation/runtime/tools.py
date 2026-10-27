"""Reuse native file/process tools under a task-local, fail-closed boundary."""
from functools import wraps
from inspect import signature

from features.evaluation.runtime.runtime import CURRENT_EVALUATION_SCOPE


_DESCRIPTIONS = {
    "Bash": "Execute a shell command. A running result must be continued using write_stdin; do not restart it.",
    "write_stdin": "Wait for a Bash process handle and return only new output. Use an empty chars value to wait.",
    "Read": "Read a file with line numbers. Use offset and limit for pagination; read fully before editing.",
    "Write": "Create a UTF-8 text file or overwrite one after Read. Concurrently changed files require another Read.",
    "Edit": "Replace text in a file after Read. Concurrently changed files require another Read.",
    "Glob": "Find files by glob pattern, sorted by modification time with page and limit pagination.",
    "Grep": "Search file contents by regular expression with ripgrep, or grep when ripgrep is absent.",
}


def scoped_tool(tool, scope):
    from core.llm.factory.tools.definitions import function_tool
    func = tool._func
    original_signature = signature(func)

    @wraps(func)
    async def scoped(*args, **kwargs):
        from core.config.local_mode import local_mode_enabled
        from core.llm.tools._common import resp_json
        if local_mode_enabled():
            return resp_json({"error": "Evaluation requires container execution, not local mode"})
        bound = original_signature.bind(*args, **kwargs)
        if bound.arguments.get("register_as_artifact"):
            return resp_json({"error": "Evaluation outputs must remain in the task sandbox"})
        token = CURRENT_EVALUATION_SCOPE.set(scope)
        try:
            return await func(*args, **kwargs)
        finally:
            CURRENT_EVALUATION_SCOPE.reset(token)

    scoped.__doc__ = (_DESCRIPTIONS.get(tool.name, "") + " "
        + f"Native {tool.name} in the current evaluation sandbox only. "
        + "No account workspace, historical artifacts, or host filesystem access.")
    return function_tool(scoped, dispatch_spec=tool.dispatch_spec)


def register_evaluation_tools(toolkit, scope, *, read_only=False, allow_bash=True, vision_mode="none"):
    from core.config.local_mode import local_mode_enabled
    from core.llm.tools import (
        register_read, register_write,
        register_edit, register_glob, register_grep,
    )
    if local_mode_enabled():
        raise ValueError("Evaluation requires container execution, not local mode")
    collector = []
    common = dict(
        chat_id=scope.session_id, sandbox_session_id=scope.session_id,
        user_id=scope.owner_user_id,
    )
    token = CURRENT_EVALUATION_SCOPE.set(scope)
    try:
        if not read_only and allow_bash:
            from features.evaluation.runtime.process_tools import register_evaluation_process_tools
            register_evaluation_process_tools(collector, scope)
        register_read(collector, **common, vision_mode=vision_mode)
        register_glob(collector, **common)
        register_grep(collector, **common)
        if not read_only:
            register_write(collector, **common)
            register_edit(collector, **common)
    finally:
        CURRENT_EVALUATION_SCOPE.reset(token)

    toolkit.extend(scoped_tool(tool, scope) for tool in collector)
