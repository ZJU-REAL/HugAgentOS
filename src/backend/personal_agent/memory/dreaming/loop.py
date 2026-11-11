"""A small model tool loop for the Dreaming agents.

The agent factory builds a full chat agent (skills, MCP, sandbox, permissions); Dreaming
needs none of that, only a model and five file tools it owns. So this calls an
AgentScope chat model directly, in AgentScope's own context shape: one assistant
message per reply that collects its text, tool calls and the matching tool results.
Thinking blocks are dropped. Tools run in a thread because they touch the database.
"""
import asyncio
import json
import re

MAX_STEPS = 80
STEP_TIMEOUT = 180            # well under state.LEASE_SECONDS: a timed-out step still holds the lease


def build_model(provider_id=None):
    """The agent's configured provider (or the Dreaming override), deterministic, no streaming."""
    from dataclasses import replace
    from core.models.chat_models import build_model_for_mode
    from core.models.config import ModelConfigService
    service = ModelConfigService.get_instance()
    cfg = (service.resolve_provider(provider_id) if provider_id else None) or service.resolve("main_agent")
    return build_model_for_mode(replace(cfg, temperature=0, max_tokens=8000, timeout=150), mode="fast", stream=False)


async def run(model, system, task, toolbox, *, renew=None, max_steps=MAX_STEPS):
    """Drive the model until it answers without a tool call; returns that answer's text."""
    from agentscope.message import Msg, TextBlock, ToolResultBlock, ToolResultState
    messages = [Msg(name="system", role="system", content=[TextBlock(text=system)]),
                Msg(name="user", role="user", content=[TextBlock(text=task)])]
    for _ in range(max_steps):
        if renew is not None: await renew()
        async with asyncio.timeout(STEP_TIMEOUT):
            response = await model(messages, tools=toolbox.schemas())
        blocks = [block for block in response.content if getattr(block, "type", None) in ("text", "tool_call")]
        reply = Msg(name="dreaming", role="assistant", content=list(blocks))
        messages.append(reply)
        calls = [block for block in blocks if block.type == "tool_call"]
        if not calls:
            return "".join(block.text for block in blocks if block.type == "text").strip()
        for call in calls:
            output, ok = await asyncio.to_thread(toolbox.call, call.name, call.input)
            reply.content.append(ToolResultBlock(id=call.id, name=call.name, output=output,
                                                 state=ToolResultState.SUCCESS if ok else ToolResultState.ERROR))
    raise RuntimeError("Dreaming agent exceeded " + str(max_steps) + " steps")


def answer(text):
    """The first JSON object in the answer, or {} when there is none."""
    text = (text or "").strip()
    if text.startswith("```"): text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    for start in [m.start() for m in re.finditer(r"\{", text)]:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            continue
        if isinstance(value, dict): return value
    return {}
