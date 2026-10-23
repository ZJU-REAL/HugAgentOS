"""Automation installation; existing scheduler enablement remains authoritative."""
NAME = "automation"


def register_schema():
    from core.harness.registry.schema_lifecycle import register_schema_lifecycle
    from features.automation.schema import initialize_schema
    register_schema_lifecycle(NAME, initialize_schema)


def setup():
    register_schema()
    return True


def register_gateway_contributions():
    from core.harness.registry.tool_operations import ToolOperations, register_tool_operations
    from features.automation import remote_effect, tool_routing
    register_tool_operations("automation", ToolOperations(
        receipt_tools=remote_effect.RECEIPT_TOOLS,
        prepare_arguments=lambda name, args, user, chat: (tool_routing.creation_arguments(args, user, chat)
                                                        if name == "create_scheduled_task" else args),
        local_tools=frozenset(tool_routing.TOOLS),
        bind=lambda *args: remote_effect.bind_remote_effect(*args),
        lookup=lambda *args: remote_effect.lookup_remote_receipt(*args),
        remember=lambda *args: remote_effect.remember_remote_call(*args),
        dispatch_local=lambda *args, **kwargs: tool_routing.local_automation_result(*args, **kwargs),
    ))
