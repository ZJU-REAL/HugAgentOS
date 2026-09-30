"""Reuse native file/process tools under a task-local, fail-closed boundary."""
from functools import wraps
from inspect import signature

from core.llm.evaluation_runtime import CURRENT_EVALUATION_SCOPE


_DESCRIPTIONS = {
    "Bash": "Execute a shell command. A running result must be continued using write_stdin; do not restart it.",
    "write_stdin": "Wait for a Bash process handle and return only new output. Use an empty chars value to wait.",
    "Read": "Read a file with line numbers. Use offset and limit for pagination; read fully before editing.",
    "Write": "Create a UTF-8 text file or overwrite one after Read. Concurrently changed files require another Read.",
    "Edit": "Replace text in a file after Read. Concurrently changed files require another Read.",
    "Glob": "Find files by glob pattern, sorted by modification time with page and limit pagination.",
    "Grep": "Search file contents by regular expression with ripgrep, or grep when ripgrep is absent.",
}


class _EvaluationCollector:
    def __init__(self, target, scope):
        self.target = target
        self.scope = scope

    def register_tool_function(self, func, **options):
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
            token = CURRENT_EVALUATION_SCOPE.set(self.scope)
            try:
                return await func(*args, **kwargs)
            finally:
                CURRENT_EVALUATION_SCOPE.reset(token)

        # Tool schemas retain the native parameters and behavioral contracts.
        scoped.__doc__ = (
            _DESCRIPTIONS.get(func.__name__, "") + " "
            f"Native {func.__name__} in the current evaluation sandbox only. "
            "No account workspace, historical artifacts, or host filesystem access."
        )
        self.target.register_tool_function(scoped, **options)


def register_evaluation_tools(toolkit, scope, *, read_only=False, allow_bash=True, vision_mode="none"):
    from core.config.local_mode import local_mode_enabled
    from core.llm.tools import (
        ReadStateTracker, register_bash, register_read, register_write,
        register_edit, register_glob, register_grep,
    )
    if local_mode_enabled():
        raise ValueError("Evaluation requires container execution, not local mode")
    collector = _EvaluationCollector(toolkit, scope)
    common = dict(
        chat_id=scope.session_id, sandbox_session_id=scope.session_id,
        user_id=scope.owner_user_id,
    )
    token = CURRENT_EVALUATION_SCOPE.set(scope)
    try:
        if not read_only and allow_bash:
            register_bash(collector, **common, loader=None, loaded_skill_ids=set())
        state = ReadStateTracker()
        register_read(collector, **common, state=state, vision_mode=vision_mode)
        register_glob(collector, **common)
        register_grep(collector, **common)
        if not read_only:
            register_write(collector, **common, state=state, interactive=False)
            register_edit(collector, **common, state=state, interactive=False)
    finally:
        CURRENT_EVALUATION_SCOPE.reset(token)
