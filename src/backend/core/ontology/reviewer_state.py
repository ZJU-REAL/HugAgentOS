"""Verdicts, perspectives and repair limits for ontology review."""
from __future__ import annotations
from collections.abc import Awaitable, Callable
from typing import Any

VALID_VERDICTS = {"pass", "revise", "escalate"}


DEFAULT_MAX_REPAIR_ATTEMPTS = 1


PERSPECTIVES = (
    "领域合规委员：逐条核对术语、关系、禁用条件和工作流约束。",
    "证据委员：只认可工具轨迹与引用中可追溯的证据，严查无证据断言。",
    "风险委员：关注高风险误导、遗漏前提、越权建议和不可逆影响。",
)


RemediationCallback = Callable[[dict[str, Any]], Awaitable[str]]
