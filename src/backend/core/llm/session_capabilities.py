"""Chat-sticky capabilities explicitly loaded by the user.

Personal Capability Center switches control initial assembly. Once a user
explicitly loads a skill or connector in a chat, this module keeps that exact
capability expanded on later turns without changing the saved switch. Hard
availability (admin state, dependency readiness and ownership) is revalidated
on every restore.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

_ACTIVATED_SKILLS_KEY = "activated_skill_ids"
_ACTIVATED_MCPS_KEY = "activated_mcp_ids"


def _clean(values: Optional[Sequence[str]]) -> List[str]:
    return list(
        dict.fromkeys(
            str(value).strip()
            for value in (values or [])
            if isinstance(value, str) and value.strip()
        )
    )


@dataclass
class SessionActivatedCapabilities:
    skill_ids: List[str] = field(default_factory=list)
    mcp_ids: List[str] = field(default_factory=list)


def record_session_capability_activation(
    chat_id: Optional[str],
    *,
    skill_ids: Optional[Sequence[str]] = None,
    mcp_ids: Optional[Sequence[str]] = None,
) -> None:
    """Persist exact direct skill/connector selections for this chat."""
    skills = _clean(skill_ids)
    mcps = _clean(mcp_ids)
    if not chat_id or not skills and not mcps:
        return
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ChatSession
        from sqlalchemy.orm.attributes import flag_modified

        with SessionLocal() as db:
            row = db.query(ChatSession).filter(ChatSession.chat_id == chat_id).first()
            if row is None:
                return
            data = dict(row.extra_data or {})
            current_skills = _clean(data.get(_ACTIVATED_SKILLS_KEY))
            current_mcps = _clean(data.get(_ACTIVATED_MCPS_KEY))
            next_skills = list(dict.fromkeys([*current_skills, *skills]))
            next_mcps = list(dict.fromkeys([*current_mcps, *mcps]))
            if next_skills == current_skills and next_mcps == current_mcps:
                return
            data[_ACTIVATED_SKILLS_KEY] = next_skills
            data[_ACTIVATED_MCPS_KEY] = next_mcps
            row.extra_data = data
            flag_modified(row, "extra_data")
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session-capabilities] activation persist failed: %s", exc)


def resolve_session_activated_capabilities(
    *,
    user_id: str,
    chat_id: Optional[str],
) -> SessionActivatedCapabilities:
    """Restore sticky direct capabilities after rechecking all hard gates."""
    result = SessionActivatedCapabilities()
    if not user_id or not chat_id:
        return result
    try:
        from core.config.catalog_resolver import resolve_explicit_runtime_capabilities
        from core.db.engine import SessionLocal
        from core.db.models import ChatSession

        with SessionLocal() as db:
            row = db.query(ChatSession.extra_data).filter(ChatSession.chat_id == chat_id).first()
            data = row[0] if row else {}
            requested_skills = _clean((data or {}).get(_ACTIVATED_SKILLS_KEY))
            requested_mcps = _clean((data or {}).get(_ACTIVATED_MCPS_KEY))
            if not requested_skills and not requested_mcps:
                return result
            skills, mcps, unavailable_skills, unavailable_mcps = (
                resolve_explicit_runtime_capabilities(
                    db,
                    user_id,
                    skill_ids=requested_skills,
                    mcp_ids=requested_mcps,
                )
            )
            result.skill_ids = skills
            result.mcp_ids = mcps
            if unavailable_skills or unavailable_mcps:
                logger.info(
                    "[session-capabilities] sticky capability unavailable "
                    "chat=%s skills=%s mcps=%s",
                    chat_id,
                    unavailable_skills,
                    unavailable_mcps,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[session-capabilities] activation restore failed: %s", exc)
    return result


__all__ = [
    "SessionActivatedCapabilities",
    "record_session_capability_activation",
    "resolve_session_activated_capabilities",
]
