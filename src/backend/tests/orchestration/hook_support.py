"""Construct the real framework adapter with selected neutral policies."""
from core.harness.hooks import HookBus
from core.llm.runtime.agentscope_hook_adapter import AgentScopeHookAdapter


def make_adapter(*specs, **options):
    bus = HookBus()
    for spec in specs:
        bus.register(spec)
    return AgentScopeHookAdapter(bus, **options)


async def inject_vision_context(state, image_files, user_id):
    from types import SimpleNamespace
    from core.llm.context.attachment_hook import vision_evidence, payload
    from core.llm.runtime.context_hook_adapter import append_hook_context
    text = await vision_evidence(image_files, user_id, getattr(state, "vision_evidence_text", ""))
    if text:
        append_hook_context(SimpleNamespace(state=state), SimpleNamespace(data={"context_items": [
            payload(text, kind="attachment", origin="vision:transcription", trust="tool")]}))


async def run_permission(service, input_kwargs, next_handler):
    from types import SimpleNamespace
    from core.llm.tool_permissions.hook import ToolPermissionHook
    policy = ToolPermissionHook(service)
    adapter = make_adapter(policy.spec(), permission_policy=policy)
    agent = SimpleNamespace(state=SimpleNamespace(context=[], run_id="permission-test"))
    async for event in adapter.on_acting(agent, input_kwargs, next_handler):
        yield event


async def inject_attachments(agent):
    from core.llm.context.attachment_hook import attachment_context_hook
    adapter = make_adapter(attachment_context_hook())
    async def handler(**kwargs):
        if False:
            yield None
    async for _ in adapter.on_reply(agent, {}, handler):
        pass


def permission_adapter(service):
    from core.llm.tool_permissions.hook import ToolPermissionHook
    policy = ToolPermissionHook(service)
    return make_adapter(policy.spec(), permission_policy=policy)


def durable_adapter(session_factory):
    from core.llm.runtime.tool_execution_adapter import DurableToolDispatch
    adapter = make_adapter()
    adapter.tool_dispatch = DurableToolDispatch(session_factory=session_factory)
    return adapter
