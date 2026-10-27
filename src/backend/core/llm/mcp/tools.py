"""AgentScope tool boundary over a live native MCP session."""
import hashlib
import json
import math
from agentscope.tool import ToolBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from core.llm.mcp.deadline import InvocationBudget
from core.llm.mcp.callbacks import CURRENT_CALL
from core.llm.mcp.results import error_result, is_timeout, poisons_session, tool_result

MAX_QUEUED_PROGRESS = 32


def _call_key(name, arguments):
    body = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{name}\0{body}".encode()).hexdigest()


def queue_progress(queue, event):
    """Keep only the newest event per call; the stream loop drains the queue between events."""
    for index, queued in enumerate(queue):
        if queued["tool_id"] == event["tool_id"]:
            queue[index] = event
            return
    if len(queue) < MAX_QUEUED_PROGRESS:
        queue.append(event)


class SessionTool(ToolBase):
    """A run's tool over a session; the run handle decides whether it may still call."""
    is_mcp = True

    def __init__(self, resource, timeout):
        super().__init__()
        self.resource, self.mcp_name, self.timeout = resource, resource.name, timeout

    def _require_open(self):
        if self.resource.client_session_lease:
            from core.harness.registry.client_sessions import require
            require(*self.resource.client_session_lease)
        if self.resource.closed or self.resource.session.stopped:
            raise RuntimeError("MCP run lease is closed")

    async def _invoke(self, operation, *, exclusive, write_key=None):
        """Run one request; only a lost session is fatal, other failures become results."""
        self._require_open()
        resource, session = self.resource, self.resource.session
        if write_key is not None and write_key in resource.unknown_outcomes:
            return error_result("该写操作上次以相同参数调用时结果未知，已拒绝重复执行。请先核对服务端状态。",
                                server=self.mcp_name, outcome_unknown=True)
        async with session.calls.exclusive() if exclusive else session.calls.shared():
            self._require_open()
            call = resource.interactions.open_call(session) if resource.interactions else None
            token = CURRENT_CALL.set(call)
            if call is not None:
                session.in_flight.add(call)
            try:
                async with InvocationBudget(self.timeout) as budget:
                    if call is not None:
                        call.budget = budget
                    return await operation()
            except BaseException as exc:
                if poisons_session(exc):
                    session.failed = True
                    raise
                if write_key is not None and is_timeout(exc):
                    resource.unknown_outcomes.add(write_key)
                    return error_result(
                        f"MCP 写操作未在预期内完成（{type(exc).__name__}），结果未知。请核对服务端状态，不要直接重试。",
                        server=self.mcp_name, outcome_unknown=True)
                return error_result(f"MCP 调用失败：{type(exc).__name__}: {exc}"[:2000], server=self.mcp_name)
            finally:
                CURRENT_CALL.reset(token)
                if call is not None:
                    session.in_flight.discard(call)
                    await call.finish()


class RemoteTool(SessionTool):
    def __init__(self, resource, descriptor, timeout):
        super().__init__(resource, timeout)
        self.name = descriptor.name
        self.description = descriptor.description or descriptor.name
        self.input_schema = descriptor.input_schema
        self.is_read_only = bool(descriptor.annotations and descriptor.annotations.read_only_hint)
        # Read-only calls have no effects to order, so the agent may run them together.
        self.is_concurrency_safe = self.is_read_only
        self.descriptor = descriptor

    async def check_permissions(self, *_args, **_kwargs):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW if self.is_read_only else PermissionBehavior.ASK,
                                  message="MCP operation is subject to the run execution policy")

    async def call(self, **kwargs):
        from core.harness.tool_call_context import CURRENT_TOOL_CALL_ID
        tool_call_id = CURRENT_TOOL_CALL_ID.get()
        interactions = self.resource.interactions

        async def progress(value, total, message):
            if interactions is None or interactions.agent is None:
                return
            queue_progress(interactions.agent.state.pending_mcp_progress, {
                "tool_id": tool_call_id, "name": self.name, "server_id": self.mcp_name,
                "progress": value, "total": total, "message": str(message or "")[:500]})

        async def operation():
            result = await self.resource.session.client.call_tool(
                self.descriptor.name, kwargs,
                # The outer pausable budget owns execution time. A second SDK
                # wall clock would expire during legacy server-to-client elicitation.
                read_timeout_seconds=math.inf if interactions is not None else self.timeout,
                progress_callback=progress)
            return tool_result(result)

        return await self._invoke(operation, exclusive=not self.is_read_only,
                                  write_key=None if self.is_read_only else _call_key(self.name, kwargs))
