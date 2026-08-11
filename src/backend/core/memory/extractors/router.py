"""Extractor router — decide what to extract, then dispatch concurrently.

- `classify_conversation()`: which extractors this turn deserves. Every
  substantive turn nominates identity, preference and procedural (plus graph
  when deployed); the keyword cues only remain as a recall floor for turns too
  short to clear the substance bar ("我叫张三"). Task stays keyword-gated: its
  cues are explicit and it is session-scoped anyway.
- `run_extractors_with_timeout()`: run the matched extractors concurrently, each with its own timeout, merging the returns.

Why the keyword gates fell: a fact shows up in whatever words the user happened
to use, and a regex listing "我是… / 一律 / 必须先" decides *before the model
ever sees the turn* whether that turn could contain one. Everything phrased
outside the list was dropped silently and invisibly — the worst failure mode a
memory system can have, because it looks identical to a turn that genuinely had
nothing to learn. Procedural learned this first; identity and preference had the
same failure (the LLM write gate can only narrow the candidate set, so whatever
the regex failed to nominate was unrecoverable — turns stating who the user is
never produced an L1 write). The gate plus the extractors are far better judges
than the regex, and both already return empty when there is nothing worth
keeping.
"""

from __future__ import annotations

import asyncio
import logging
import re
from enum import Enum
from typing import Optional

from core.config.settings import settings
from core.memory.context import MemoryContext

logger = logging.getLogger(__name__)


class ExtractorType(Enum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    TASK = "task"
    PROCEDURAL = "procedural"
    GRAPH = "graph"


# ─── Keyword trigger conditions ─────────────────────────────────────────────────────────


_IDENTITY_CUES = re.compile(
    r"(?:我叫|我是|我在|我负责|我的岗位|我的职务|我的部门|我的处室|"
    r"我的单位|我的公司|我的团队|我的联系方式|我的邮箱|我所在|我来自|我现在在|"
    # Explicit requests like "帮我记住我..." / "记一下我的部门" also count as identity triggers
    r"(?:帮我|请|麻烦)?(?:记住|记一下|记录).*?(?:我|部门|处室|单位|岗位|团队|科室)|"
    r"I am|I work|my name|my role|my team|my position|remember that I)",
    re.IGNORECASE,
)

_PREFERENCE_CUES = re.compile(
    r"(?:喜欢|更倾向|更喜欢|偏好|请用|回答时|输出格式|以后|从现在起|"
    r"不要|少说|不用|别用|别再|简洁点|详细点|"
    r"prefer|从此|always|never)",
    re.IGNORECASE,
)

_TASK_CUES = re.compile(
    r"(?:帮我|接下来|然后|计划|分析|写一份|做一版|生成|输出|整理|"
    r"先.*再|第一步|第二步|步骤|一起做)",
    re.IGNORECASE,
)

# A turn shorter than this on either side cannot state a convention: a two-word
# request with a one-line answer has no room for "how such things are done", and
# running the extractor on it would only buy noise at the cost of an LLM call.
_SUBSTANTIVE_USER_CHARS = 8
_SUBSTANTIVE_ASSISTANT_CHARS = 30


def classify_conversation(user_msg: str, assistant_msg: str) -> set[ExtractorType]:
    """Classify which extractors should run for this conversation turn.

    Returning an empty set means "nothing worth extracting" — skip all LLM calls directly.
    """
    if not user_msg or not user_msg.strip():
        return set()

    classes: set[ExtractorType] = set()

    # Keyword cues are a recall floor, not the gatekeeper: they catch turns too
    # short to clear the substance bar below ("我叫张三" is 4 chars). Task keeps
    # them as its only trigger — it is session-scoped and its cues are explicit.
    if _IDENTITY_CUES.search(user_msg):
        classes.add(ExtractorType.IDENTITY)
    if _PREFERENCE_CUES.search(user_msg):
        classes.add(ExtractorType.PREFERENCE)
    if _TASK_CUES.search(user_msg):
        classes.add(ExtractorType.TASK)

    # No keyword gate past this point, only a substance floor: the LLM write
    # gate and the extractors decide what the turn actually contained. A regex
    # cannot, and whatever it missed was dropped invisibly — the gate can only
    # narrow this set, so anything not nominated here is unrecoverable.
    if (
        len(user_msg.strip()) >= _SUBSTANTIVE_USER_CHARS
        and len((assistant_msg or "").strip()) >= _SUBSTANTIVE_ASSISTANT_CHARS
    ):
        classes.add(ExtractorType.PROCEDURAL)
        classes.add(ExtractorType.IDENTITY)
        classes.add(ExtractorType.PREFERENCE)
        # L3 has the same substance floor, but is deployment-gated on Neo4j.
        if settings.memory.graph_enabled:
            classes.add(ExtractorType.GRAPH)

    return classes


# ─── Concurrent dispatch ───────────────────────────────────────────────────────────────


async def run_extractors_with_timeout(
    classes: set[ExtractorType],
    user_message: str,
    assistant_message: str,
    ctx: MemoryContext,
    timeout_s: int = 30,
    recent_trajectory: str = "",
    verified_correction: bool = False,
) -> dict[ExtractorType, Optional[dict]]:
    """Run the matched extractors concurrently, each with its own timeout.

    Returns { ExtractorType: dict or None }, where None means that extractor failed / timed out / produced an invalid result.
    """
    if not classes:
        return {}

    from core.memory.extractors import graph, identity, preference, procedural, task

    runners: dict[ExtractorType, callable] = {
        ExtractorType.IDENTITY: identity.extract,
        ExtractorType.PREFERENCE: preference.extract,
        ExtractorType.TASK: task.extract,
        ExtractorType.PROCEDURAL: procedural.extract,
        ExtractorType.GRAPH: graph.extract,
    }

    async def _wrap(et: ExtractorType):
        try:
            if et is ExtractorType.PROCEDURAL:
                return et, await procedural.extract(
                    user_message,
                    assistant_message,
                    timeout_s,
                    recent_trajectory=recent_trajectory,
                    verified_correction=verified_correction,
                )
            return et, await runners[et](user_message, assistant_message, timeout_s)
        except Exception as exc:
            logger.warning("[extractor:%s] failed: %s", et.value, exc)
            return et, None

    tasks = [_wrap(et) for et in classes if et in runners]
    results: dict[ExtractorType, Optional[dict]] = {}
    for completed in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(completed, BaseException):
            logger.warning("[extractor:router] task crashed: %s", completed)
            continue
        et, data = completed
        results[et] = data
    return results
