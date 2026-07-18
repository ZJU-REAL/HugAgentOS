"""Skill selector - dynamically select skills based on user intent.

Implements progressive disclosure: only load skills relevant to the user's query.
"""

from __future__ import annotations

import json
from typing import List, Optional

from agentscope.model import OpenAIChatModel

from core.agent_skills.registry import AgentSkillMetadata
from core.agent_skills.loader import get_skill_loader


def select_skills_for_query(
    *,
    user_query: str,
    available_skill_ids: List[str],
    enabled_skill_ids: List[str],
    model: Optional[OpenAIChatModel] = None,
    max_skills: int = 5,
) -> List[str]:
    """Select relevant skills for a user query using LLM.

    Args:
        user_query: The user's input/question
        available_skill_ids: Skills that can be selected (from AgentSpec)
        enabled_skill_ids: Skills that are enabled in catalog
        model: LLM model for selection (if None, returns all available skills)
        max_skills: Maximum number of skills to select

    Returns:
        List of selected skill IDs
    """
    # Filter: only consider skills that are both available and enabled
    candidate_ids = set(available_skill_ids) & set(enabled_skill_ids)
    if not candidate_ids:
        return []

    # Load metadata for candidates
    loader = get_skill_loader()
    all_metadata = loader.load_all_metadata()
    candidates = [all_metadata[sid] for sid in candidate_ids if sid in all_metadata]

    if not candidates:
        return []

    # If no model provided, return all candidates (fallback)
    if model is None:
        return sorted([s.id for s in candidates])[:max_skills]

    # Use LLM to select relevant skills
    return _llm_select_skills(
        user_query=user_query,
        candidates=candidates,
        model=model,
        max_skills=max_skills,
    )


def _llm_select_skills(
    *,
    user_query: str,
    candidates: List[AgentSkillMetadata],
    model: OpenAIChatModel,
    max_skills: int,
) -> List[str]:
    """Use LLM to select relevant skills based on descriptions."""

    # Build skill descriptions for LLM
    skill_descriptions = []
    for skill in candidates:
        skill_descriptions.append(
            {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description[:1024],  # Truncate like deepagents
                "tags": skill.tags,
            }
        )

    # Prompt for LLM skill selection
    system_prompt = """You are a skill selector. Given a user query and available skills, select the most relevant skills.

Return a JSON array of skill IDs, ordered by relevance (most relevant first).
Only select skills that are clearly relevant to the user's query.
Maximum {max_skills} skills.

Example output:
["skill-id-1", "skill-id-2"]
""".format(
        max_skills=max_skills
    )

    user_prompt = f"""User query: {user_query}

Available skills:
{json.dumps(skill_descriptions, indent=2, ensure_ascii=False)}

Select relevant skill IDs (return JSON array):"""

    try:
        import asyncio

        async def _call():
            # AgentScope 2.0: model.__call__ takes list[Msg] (not list[dict]).
            from agentscope.message import Msg, TextBlock
            return await model(
                messages=[
                    Msg(name="system", role="system",
                        content=[TextBlock(type="text", text=system_prompt)]),
                    Msg(name="user", role="user",
                        content=[TextBlock(type="text", text=user_prompt)]),
                ],
            )

        response = asyncio.run(_call())

        # Parse response - extract text from content blocks (2.0: block is a pydantic object)
        raw_content = response.content
        if isinstance(raw_content, list):
            text_parts = []
            for block in raw_content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif getattr(block, "type", None) == "text":
                    text_parts.append(getattr(block, "text", ""))
            content = "".join(text_parts).strip()
        else:
            content = str(raw_content).strip()

        # Try to extract JSON array
        if content.startswith("[") and content.endswith("]"):
            selected = json.loads(content)
        else:
            # Try to find JSON array in response
            start = content.find("[")
            end = content.rfind("]") + 1
            if start >= 0 and end > start:
                selected = json.loads(content[start:end])
            else:
                # Fallback: return all candidates
                return sorted([s.id for s in candidates])[:max_skills]

        # Validate selected IDs
        valid_ids = {s.id for s in candidates}
        result = [sid for sid in selected if sid in valid_ids]
        return result[:max_skills]

    except Exception:
        # Fallback: return all candidates
        return sorted([s.id for s in candidates])[:max_skills]
