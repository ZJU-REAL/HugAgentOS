"""Skill icon service.

Skill icons are stored in the payload of ``ContentBlock(id="skill_icons")``, structured
as ``{skill_id: icon}``. icon takes one of three values: ``preset:<key>`` (a key in the
frontend's built-in SVG icon library), a ``http(s)://...`` URL, or a
``data:image/...;base64,...`` inline small image (a data-URI converted from a user upload).

Deliberately not adding a column to the ``admin_skills`` table: this repo's alembic is in
a multi-head forked state, so an add-column migration is risky; ContentBlock is the
existing key-value JSON store (docs_updates / prompt_versions etc. all use it), zero
schema change.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.db.models import ContentBlock

logger = logging.getLogger(__name__)

ICON_BLOCK_ID = "skill_icons"
MAX_ICON_LEN = 200_000  # data-URI limit (~150KB original image), prevents stuffing an oversized image into the catalog

# category → built-in icon preset key (gives a meaningful default icon by category when a marketplace skill is installed).
# Key names must match the PRESET keys in the frontend's skillIcons.tsx.
CATEGORY_PRESET: Dict[str, str] = {
    # Current marketplace's 8 major categories
    "写作助手": "pen",
    "文档处理": "doc",
    "数据分析": "chart",
    "政策产业": "policy",
    "营销创意": "megaphone",
    "法务合规": "scale",
    "办公效率": "flow",
    "研发效率": "code",
    "社区共享": "book",
    # Historical category names (compatible with installed skills / old categories filled in by community applications)
    "公文写作": "doc",
    "写作润色": "pen",
    "可视化绘图": "flow",
    "创意设计": "image",
    "流程效率": "flow",
    "数据查询": "data",
    "政策服务": "policy",
    "营销策划": "megaphone",
    "财务分析": "finance",
    "知识管理": "book",
    "商业策略": "target",
    "数据安全": "shield",
    "产业分析": "chart",
    "政务服务": "policy",
    "翻译语言": "doc",
    "项目管理": "flow",
}


def preset_for_category(category: Optional[str]) -> str:
    """category → preset icon string (``preset:<key>``); unknown categories fall back to the generic doc."""
    return "preset:" + CATEGORY_PRESET.get((category or "").strip(), "doc")


def get_skill_icons(db: Session) -> Dict[str, str]:
    """Load the full skill icon mapping ``{skill_id: icon}`` (fault-tolerant, empty dict if absent)."""
    try:
        row = db.query(ContentBlock).filter(ContentBlock.id == ICON_BLOCK_ID).first()
        if row and isinstance(row.payload, dict):
            return dict(row.payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_skill_icons failed: %s", exc)
    return {}


def get_skill_icon(db: Session, skill_id: str) -> str:
    return get_skill_icons(db).get(skill_id, "")


def set_skill_icon(db: Session, skill_id: str, icon: Optional[str]) -> str:
    """Set/clear a single skill icon. An empty string/None ``icon`` is treated as clear (fall back to default). Returns the final value."""
    icon = (icon or "").strip()
    if len(icon) > MAX_ICON_LEN:
        from core.infra.exceptions import BadRequestError
        raise BadRequestError(message=f"图标过大（上限 {MAX_ICON_LEN // 1000}KB），请换更小的图或用内置图标")

    row = db.query(ContentBlock).filter(ContentBlock.id == ICON_BLOCK_ID).first()
    icons = dict(row.payload) if (row and isinstance(row.payload, dict)) else {}
    if icon:
        icons[skill_id] = icon
    else:
        icons.pop(skill_id, None)
    if row is not None:
        row.payload = icons
        flag_modified(row, "payload")
    else:
        db.add(ContentBlock(id=ICON_BLOCK_ID, payload=icons))
    db.commit()
    return icon


def delete_skill_icon(db: Session, skill_id: str) -> None:
    """When deleting a skill, remove its icon entry (ignored if absent)."""
    try:
        row = db.query(ContentBlock).filter(ContentBlock.id == ICON_BLOCK_ID).first()
        if row and isinstance(row.payload, dict) and skill_id in row.payload:
            icons = dict(row.payload)
            icons.pop(skill_id, None)
            row.payload = icons
            flag_modified(row, "payload")
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("delete_skill_icon failed: %s", exc)
