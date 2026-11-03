"""Text helpers for a task's result: the clock line, export links and the fallback final answer.

The Worker itself runs on the core chat runtime (``personal_agent.worker``)."""
import re
from collections import Counter
from datetime import datetime

MAX_SEGMENT = 30000

def runaway(text):
    if len(text) > MAX_SEGMENT: return True
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) >= 30]
    return bool(lines) and Counter(lines).most_common(1)[0][1] >= 4

def has_cjk(text):
    return bool(re.search(r"[一-鿿]", text or ""))

def clean_final(text, goal):
    """Fallback when the Worker gives no final report: drop leading process narration."""
    text = (text or "").strip()
    parts = re.split(r"\n-{3,}\n", text)
    if len(parts) > 1 and has_cjk(goal) and not has_cjk(parts[0]):
        return "\n---\n".join(parts[1:]).strip()
    if has_cjk(goal):
        paragraphs = text.split("\n\n")
        while len(paragraphs) > 1 and not has_cjk(paragraphs[0]):
            paragraphs.pop(0)
        return "\n\n".join(paragraphs).strip()
    return text

FINALIZE = """You receive a task goal and the last text that an agent wrote. The agent did not deliver a clean answer.
Write the final answer for the user. Use the language of the task goal. Keep facts, numbers and links from the text.
Remove process narration, plans, self-talk and repeated text. Do not invent results. If the text shows no result,
say in one or two sentences what is known and that the task did not finish. Return only the answer."""

async def finalize(goal, text, provider_id=None):
    """One bounded, tool-free model call. Falls back to local cleanup."""
    import asyncio
    from dataclasses import replace
    try:
        from agentscope.message import Msg, TextBlock
        from core.models.chat_models import build_model_for_mode
        from core.models.config import ModelConfigService
        service = ModelConfigService.get_instance()
        cfg = (service.resolve_provider(provider_id) if provider_id else None) or service.resolve("main_agent")
        model = build_model_for_mode(replace(cfg, temperature=0, max_tokens=2000, timeout=60), mode="fast", stream=False)
        async with asyncio.timeout(70):
            result = await model([Msg(name="system", role="system", content=[TextBlock(text=FINALIZE)]),
                Msg(name="user", role="user", content=[TextBlock(text="Task goal:\n" + goal[:4000] + "\n\nAgent text:\n" + text[-12000:])])])
        answer = "".join(block.text for block in result.content if isinstance(block, TextBlock)).strip()
        if answer and not runaway(answer): return answer
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Personal final answer cleanup failed (%s)", type(exc).__name__)
    return clean_final(text, goal)

def with_exports(final, exports, run_id):
    """Append links for files exported in this run that the answer does not mention."""
    missing = [item for item in exports if item.get("run_id") == run_id and item["url"] not in final]
    if not missing: return final
    return final.rstrip() + "\n\n" + "\n".join("📎 [" + item["name"] + "](" + item["url"] + ")" for item in missing)

def clock(config):
    from personal_agent.scheduling.schedules import user_zone, zone
    name = user_zone(config)
    return "Current time for the user: " + datetime.now(zone(name)).strftime("%Y-%m-%d %H:%M (%A)") + ", timezone " + name + ".\n"
