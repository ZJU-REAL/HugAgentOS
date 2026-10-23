"""SDK result buffering and conversion around neutral tool hooks."""
import json
from contextlib import aclosing
from agentscope.tool._response import ToolChunk, ToolResponse
from core.harness.events import thaw_value
from core.harness.hooks import HookStage
from core.llm.citations.hook import citation_candidate
from core.llm.citations.allocator import resolve_allocator
from core.llm.runtime.context_hook_adapter import append_hook_context
from core.llm.runtime.tool_hook_adapter import tool_admission, permission_dispatch


async def run_tool(adapter, agent, input_kwargs, next_handler, plain):
    call = input_kwargs.get("tool_call")
    raw = getattr(call, "input", "") or "{}"
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        args = {"_raw": str(raw)}
    name = str(getattr(call, "name", "") or "tool")
    final = await tool_admission(adapter, agent, call, args, plain)
    buffered = []
    buffer_result = citation_candidate(name)
    exposed = False
    if final is not None:
        buffered.append(ToolChunk(content=list(final.content), state=final.state, metadata=dict(final.metadata or {})))
    else:
        async with aclosing(permission_dispatch(adapter, agent, input_kwargs, next_handler)) as stream:
            async for event in stream:
                if isinstance(event, ToolResponse):
                    final = event
                elif buffer_result:
                    buffered.append(event)
                else:
                    exposed = True
                    yield event
    if buffer_result:
        resolve_allocator(agent)
    observed = await adapter._stage(
        HookStage.AFTER_TOOL, agent, operation_name=name,
        data={"tool_result": plain(final), "context_items": [], "result_exposed": exposed,
              "metadata": {"tool_call_id": str(getattr(call, "id", "") or "")}},
    )
    append_hook_context(agent, observed)
    patched = thaw_value(observed.data.get("tool_result"))
    if patched is not None and patched != plain(final):
        final = ToolResponse.model_validate(patched)
        yield ToolChunk(content=list(final.content), state=final.state, metadata=dict(final.metadata or {}))
    else:
        for event in buffered:
            yield event
    if final is not None:
        yield final
