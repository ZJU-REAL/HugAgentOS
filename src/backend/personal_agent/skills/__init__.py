"""Hugo's built-in skills: one directory per skill, each with a SKILL.md.

They are not part of the platform's skill catalog, so they never show in the capability center or
skill management. Every Hugo gets them: capability sync copies them to /workspace/skills/<id>/,
where the agent reads them like any other file. Add a Hugo built-in skill here, not under
extensions/skills/.
"""
import functools
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent


@functools.cache
def builtin_skills():
    """[{"id", "name", "description", "dir"}] in a stable order, read once per process: the files
    only change with a deploy."""
    from core.capabilities.skills.registry import split_frontmatter
    out = []
    for base in sorted(p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()):
        try:
            frontmatter, _ = split_frontmatter((base / "SKILL.md").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - without frontmatter the id still names the skill
            frontmatter = {}
        out.append({"id": base.name, "name": str(frontmatter.get("display_name") or base.name),
                    "description": str(frontmatter.get("description") or ""), "dir": base})
    return tuple(out)
