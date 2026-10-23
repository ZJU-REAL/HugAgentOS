"""SDK execution adapter for a tool-free independent ontology review."""
from typing import Any
from core.harness.registry.capability_runtime import child_scope

async def run_text_agent(
    prompt: str,
    *,
    user_id: str,
    model_name: str | None,
    model_provider_id: str | None,
    runtime: dict[str, Any],
    capability_scope: str = "",
) -> str:
    from core.llm.factory import create_agent_executor
    from core.llm.mcp.manager import close_clients
    from core.llm.runtime.streaming import StreamingAgent

    agent, clients = await create_agent_executor(
        disable_tools=True,
        enabled_skill_ids=[],
        current_user_id=user_id,
        model_name=model_name,
        model_provider_id=model_provider_id,
        chat_mode="fast",
        max_iters=2,
        isolated=True,
        ontology_runtime=runtime,
        capability_scope=child_scope(capability_scope, "ontology-model", "text"),
    )
    stream = StreamingAgent(agent, clients)
    text = ""
    try:
        async for event_type, payload in stream.stream(
            [{"role": "user", "content": prompt}],
            {
                "user_id": user_id,
                "model_name": model_name or "",
                "model_provider_id": model_provider_id or "",
                "enable_thinking": False,
                "chat_mode": "fast",
                "ontology_enabled": True,
                "ontology_runtime": runtime,
            },
        ):
            if event_type == "text_delta":
                text += payload
            elif event_type == "error":
                raise RuntimeError(str(payload))
    finally:
        await close_clients(clients)
    return text
