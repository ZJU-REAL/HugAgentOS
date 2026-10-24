"""Process tools owned by an evaluation lease, without ordinary-chat state."""
from core.llm.factory.tools.definitions import function_tool
from core.llm.tools._tool_helpers import resp_json as _resp_json

def register_evaluation_process_tools(toolkit, scope):
    """An evaluation controller owns completion and destroys its task sandbox."""
    import os
    if os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() != "true":
        return
    async def Bash(command: str, timeout: int | None = None,
                   yield_time_ms: int = 60000):
        from core.sandbox import ProcessRequest, get_sandbox_provider
        if not command.strip():
            return _resp_json({"error":"command 不能为空"})
        if timeout is not None and timeout <= 0:
            return _resp_json({"error":"timeout 必须为正数"})
        result = await get_sandbox_provider().start_process(ProcessRequest(
            script_content=command, script_name="_bash.sh", language="bash",
            session_id=scope.session_id, user_id=scope.owner_user_id, timeout=timeout),
            yield_time_ms=yield_time_ms)
        return _resp_json(result)

    async def write_stdin(session_id: str, chars: str = "",
                          yield_time_ms: int = 60000):
        from core.sandbox import get_sandbox_provider
        result = await get_sandbox_provider().write_stdin(session_id,
            sandbox_session_id=scope.session_id, user_id=scope.owner_user_id,
            chars=chars, yield_time_ms=yield_time_ms)
        return _resp_json(result)

    toolkit.append(function_tool(write_stdin))
    toolkit.append(function_tool(Bash))
