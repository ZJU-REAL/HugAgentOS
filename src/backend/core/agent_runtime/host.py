"""Compose the top-level chat flow with durable compaction boundaries."""

def stream_chat_workflow(**kwargs):
    from core.agent_runtime.integrations.compaction_stream import stream_with_compaction
    from core.agent_runtime.runner import astream_chat_workflow
    return stream_with_compaction(astream_chat_workflow, **kwargs)


def register():
    from core.harness.registry.chat_execution import register_chat_execution
    register_chat_execution(stream_chat_workflow)
