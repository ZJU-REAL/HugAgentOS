"""skill gen responsibilities."""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Sequence, Tuple
import logging
logger = logging.getLogger(__name__)
from core.evolution.generators import skill_drafting as skill_drafting_module
from core.evolution.generators import skill_evidence as skill_evidence_module
from core.evolution.generators import skill_gen_state as skill_gen_state_module

async def draft_skill(pack: skill_evidence_module.EvidencePack) -> skill_evidence_module.DraftResult:
    """Draft and validate, retrying with the specific violations.

    Returns ``ok=False`` rather than raising. A skill that cannot be written to
    the required standard must produce no candidate at all: shipping a draft
    that failed validation would put exactly the document the constraints exist
    to prevent in front of the model.
    """
    from core.memory.extractors._base import run_llm_with_prompt

    violations: List[str] = []
    for attempt in range(1, skill_gen_state_module.MAX_ATTEMPTS + 1):
        prompt = skill_drafting_module.build_prompt(pack, violations)
        raw = await run_llm_with_prompt(prompt, timeout_s=skill_gen_state_module.DRAFT_TIMEOUT_S, max_tokens=2400)
        if raw is None:
            return skill_evidence_module.DraftResult(
                ok=False,
                violations=["generator_unavailable"],
                attempts=attempt,
                generator_unavailable=True,
            )

        display_name, description, body = skill_drafting_module.parse_draft(raw)
        violations = skill_drafting_module.validate_draft(
            skill_id=pack.skill_id,
            display_name=display_name,
            description=description,
            body=body,
            allowed_tools=pack.tools,
        )
        if not violations:
            return skill_evidence_module.DraftResult(
                ok=True,
                body=body,
                display_name=display_name[:60],
                description=description[:200].replace("\n", " "),
                attempts=attempt,
            )
        logger.info(
            "[skill-gen] draft attempt %d rejected for %s: %s",
            attempt, pack.skill_id, violations,
        )

    return skill_evidence_module.DraftResult(ok=False, violations=violations, attempts=skill_gen_state_module.MAX_ATTEMPTS)


async def generate_skill_document(
    *,
    skill_id: str,
    episodes: Sequence[Dict[str, Any]],
    ordering_constraints: Sequence[Dict[str, Any]] = (),
    procedures: Sequence[Dict[str, str]] = (),
    rationale: str = "",
) -> Tuple[Optional[Dict[str, str]], skill_evidence_module.DraftResult]:
    """All three stages. Returns ``(document, result)``; document is ``None`` on failure."""
    pack = skill_evidence_module.build_evidence_pack(
        skill_id=skill_id,
        episodes=episodes,
        ordering_constraints=ordering_constraints,
        procedures=procedures,
    )
    if not pack.tools and not pack.procedures:
        return None, skill_evidence_module.DraftResult(ok=False, violations=["no_evidence_to_write_from"])

    result = await draft_skill(pack)
    if not result.ok:
        return None, result

    return (
        {
            "skill_id": skill_id,
            "display_name": result.display_name,
            "description": result.description,
            "content": skill_drafting_module.render_skill_markdown(
                skill_id=skill_id,
                display_name=result.display_name,
                description=result.description,
                body=result.body,
                support=pack.support,
                rationale=rationale,
            ),
            "allowed_tools": list(pack.tools),
        },
        result,
    )
