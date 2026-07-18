"""Extractor router — classify the conversation + concurrently dispatch 4 extractors.

- `classify_conversation()`: lightweight keyword-based classification (no LLM call); on an empty hit set, skip directly.
- `run_extractors_with_timeout()`: run the matched extractors concurrently, each with its own timeout, merging the returns.
"""

from __future__ import annotations

import asyncio
import logging
import re
from enum import Enum
from typing import Optional

from core.memory.context import MemoryContext

logger = logging.getLogger(__name__)


class ExtractorType(Enum):
    IDENTITY = "identity"
    PREFERENCE = "preference"
    FACT = "fact"
    TASK = "task"


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

_FACT_CUES = re.compile(
    r"(?:查询|查一下|查下|帮我查|数据|统计|指标|数值|占比|同比|环比|"
    r"GDP|财政|收入|营收|利润|增速|报告|年报|季报|文件|通知|批复|"
    r"营收|毛利|KPI|OKR|项目|客户)",
    re.IGNORECASE,
)

_TASK_CUES = re.compile(
    r"(?:帮我|接下来|然后|计划|分析|写一份|做一版|生成|输出|整理|"
    r"先.*再|第一步|第二步|步骤|一起做)",
    re.IGNORECASE,
)


def classify_conversation(user_msg: str, assistant_msg: str) -> set[ExtractorType]:
    """Classify which extractors should run for this conversation turn.

    Returning an empty set means "nothing worth extracting" — skip all LLM calls directly.
    """
    if not user_msg or not user_msg.strip():
        return set()

    classes: set[ExtractorType] = set()
    joint = (user_msg or "") + "\n" + (assistant_msg or "")

    if _IDENTITY_CUES.search(user_msg):
        classes.add(ExtractorType.IDENTITY)
    if _PREFERENCE_CUES.search(user_msg):
        classes.add(ExtractorType.PREFERENCE)
    # fact requires the assistant to have actually answered something (>30 chars), to avoid storing "I don't know either"
    if _FACT_CUES.search(joint) and len(assistant_msg or "") > 30:
        classes.add(ExtractorType.FACT)
    if _TASK_CUES.search(user_msg):
        classes.add(ExtractorType.TASK)
    return classes


# ─── Concurrent dispatch ───────────────────────────────────────────────────────────────


async def run_extractors_with_timeout(
    classes: set[ExtractorType],
    user_message: str,
    assistant_message: str,
    ctx: MemoryContext,
    timeout_s: int = 30,
) -> dict[ExtractorType, Optional[dict]]:
    """Run the matched extractors concurrently, each with its own timeout.

    Returns { ExtractorType: dict or None }, where None means that extractor failed / timed out / produced an invalid result.
    """
    if not classes:
        return {}

    from core.memory.extractors import identity, preference, fact, task

    runners: dict[ExtractorType, callable] = {
        ExtractorType.IDENTITY: identity.extract,
        ExtractorType.PREFERENCE: preference.extract,
        ExtractorType.FACT: fact.extract,
        ExtractorType.TASK: task.extract,
    }

    async def _wrap(et: ExtractorType):
        try:
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
