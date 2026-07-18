"""Sub-agent marketplace categories (single source of truth) + Cherry Studio group → category mapping.

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

# Cherry Studio's group tags (dozens of them) → normalized mapping to this project's 9 categories.
# Only used by scripts/import_cherry_agents.py during import; an unmatched group falls to 「通用助手」.
CHERRY_GROUP_TO_CATEGORY = {
    # General
    "通用": "通用助手",
    "工具": "通用助手",
    "百科": "通用助手",
    # Workplace / office
    "职业": "职场办公",
    "办公": "职场办公",
    "管理": "职场办公",
    "咨询": "职场办公",
    # Business analysis
    "商业": "商业分析",
    "营销": "商业分析",
    "金融": "商业分析",
    "分析": "商业分析",
    "点评": "商业分析",
    # Data analysis (Cherry has no dedicated group; decided secondarily by keywords in the import script)
    # R&D / programming
    "编程": "研发编程",
    "科学": "研发编程",
    # Translation / writing
    "翻译": "翻译写作",
    "写作": "翻译写作",
    "文案": "翻译写作",
    "语言": "翻译写作",
    "学术": "翻译写作",
    # Creative design
    "创意": "创意设计",
    "设计": "创意设计",
    "艺术": "创意设计",
    "音乐": "创意设计",
    # Policy / legal
    "法律": "政策法务",
    # Education / research
    "教育": "教育科研",
}


def validate_category(category: str) -> str:
    """Validate and return a legal category; anything not in the fixed set 400s directly."""
    category = (category or "").strip()
    if category not in AGENT_MARKETPLACE_CATEGORIES:
        raise BadRequestError(
            message=f"请从固定分类中选择：{'、'.join(AGENT_MARKETPLACE_CATEGORIES)}"
        )
    return category
