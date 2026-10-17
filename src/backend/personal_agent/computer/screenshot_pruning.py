"""Keep only recent desktop screenshots in a Personal Agent model context.

Each observation adds a full-screen image. After many steps, old images use most of the
context and the model output degrades. The latest screenshots carry the current state.
"""
import logging

from agentscope.message import TextBlock, ToolResultBlock
from agentscope.middleware import MiddlewareBase

from core.llm.compacting_agent import CompactingAgent

log = logging.getLogger(__name__)
KEEP = 2
PLACEHOLDER = "[An older screenshot was removed. Use the latest observation.]"

is_image = CompactingAgent._is_image

def prune(messages, keep=KEEP):
    """Replace all but the newest `keep` tool-result images with a short text. Returns the count."""
    seen, removed = 0, 0
    for message in reversed(messages):
        content = message.content if isinstance(message.content, list) else []
        changed = False
        blocks = []
        for block in reversed(content):
            if isinstance(block, ToolResultBlock) and isinstance(block.output, list):
                output = []
                for item in reversed(block.output):
                    if is_image(item):
                        seen += 1
                        if seen > keep:
                            item, removed, changed = TextBlock(text=PLACEHOLDER), removed + 1, True
                    output.append(item)
                if changed:
                    block = block.model_copy(update={"output": list(reversed(output))})
            blocks.append(block)
        if changed:
            message.content = list(reversed(blocks))
    return removed

class ScreenshotPruningMiddleware(MiddlewareBase):
    async def on_reasoning(self, agent, input_kwargs, next_handler):
        try:
            removed = prune(agent.state.context)
            if removed: log.debug("Removed %d older screenshots from context", removed)
        except Exception as exc:  # noqa: BLE001 - never block the model call
            log.warning("Screenshot pruning failed (%s)", type(exc).__name__)
        async for event in next_handler(**input_kwargs):
            yield event
