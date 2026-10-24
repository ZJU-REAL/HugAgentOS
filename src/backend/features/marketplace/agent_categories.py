"""Sub-agent marketplace categories (single source of truth).

The sub-agent marketplace categories are **independent of the skill marketplace's 8
categories** (role-positioning semantics fit agents better). The preset list, community
listing applications, and admin review category changes may only take these 9 values;
the frontend mirror is ``AGENT_MARKETPLACE_CATEGORIES`` in ``utils/constants.ts``.
"""

from __future__ import annotations

from typing import List

from core.infra.exceptions import BadRequestError

# Sub-agent marketplace fixed categories (stable order, frontend mirror must match)
AGENT_MARKETPLACE_CATEGORIES: List[str] = [
    "通用助手",
    "职场办公",
    "商业分析",
    "数据分析",
    "研发编程",
    "翻译写作",
    "创意设计",
    "政策法务",
    "教育科研",
]

DEFAULT_AGENT_CATEGORY = "通用助手"


def validate_category(category: str) -> str:
    """Validate and return a legal category; anything not in the fixed set 400s directly."""
    category = (category or "").strip()
    if category not in AGENT_MARKETPLACE_CATEGORIES:
        raise BadRequestError(
            message=f"请从固定分类中选择：{'、'.join(AGENT_MARKETPLACE_CATEGORIES)}"
        )
    return category
