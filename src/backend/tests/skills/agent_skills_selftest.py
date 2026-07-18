"""Selftest for agent skills registry."""

from __future__ import annotations

from core.agent_skills.loader import get_skill_loader
from core.agent_skills.registry import render_skills_prompt


def main() -> int:
    loader = get_skill_loader()
    metadata_map = loader.load_all_metadata()

    # Load full specs for all skills
    skills = []
    for skill_id in metadata_map:
        spec = loader.load_skill_full(skill_id)
        if spec:
            skills.append(spec)

    assert skills, "expected builtin skill specs"
    ids = {s.id for s in skills}
    assert "capability-guide-brief" in ids
    assert "quick-material-analysis" in ids
    assert "process-guidance" in ids
    prompt = render_skills_prompt(skills)
    assert "Skill:" in prompt
    print("agent_skills_selftest: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
