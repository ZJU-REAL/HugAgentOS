"""Bound desktop screenshots in plain model-context snapshots."""
from core.harness.events import thaw_value
from core.harness.hooks import Decision, FailureMode, HookSpec, HookStage

KEEP = 2
PLACEHOLDER = "[An older screenshot was removed. Use the latest observation.]"


def prune(messages, keep=KEEP):
    """Mutate plain message dictionaries, retaining the newest tool images."""
    seen = removed = 0
    for message in reversed(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            output = block.get("output")
            if not isinstance(output, list):
                continue
            for index in range(len(output) - 1, -1, -1):
                item = output[index]
                if isinstance(item, dict) and item.get("type") == "data" and str((item.get("source") or {}).get("media_type") or "").startswith("image/"):
                    seen += 1
                    if seen > keep:
                        output[index] = {"type": "text", "text": PLACEHOLDER}
                        removed += 1
    return removed


def trim_screenshots(invocation):
    if invocation.operation_name != "agent_reasoning":
        return Decision()
    messages = thaw_value(invocation.data.get("context") or ())
    return Decision.modify({"context": messages}) if prune(messages) else Decision()


def screenshot_pruning_hook():
    return HookSpec("desktop_screenshot_pruning", HookStage.TRANSFORM_CONTEXT,
                    trim_screenshots, priority=900, failure_mode=FailureMode.OPEN,
                    mutable_fields=frozenset({"context"}))
