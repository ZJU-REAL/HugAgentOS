"""Find the executable scripts a skill ships when it has no _scripts.json."""
from __future__ import annotations
from pathlib import Path
from typing import Dict, List
from core.capabilities.skills.backends import SkillFileInfo


SCRIPT_EXTENSIONS = {".py", ".js", ".sh", ".r", ".R"}


def bound_skill_id(skill_info: SkillFileInfo, declared_id: str) -> str:
    # Capability names come from the resolved view, independently of the file's
    # original frontmatter. Ordinary source files keep their existing ID rules.
    if skill_info.source_name == "prepared" or (skill_info.origin or {}).get("install_id"):
        return skill_info.skill_id
    return declared_id


def auto_detect_scripts(
    extra_files: Dict[str, str],
    skill_info: "SkillFileInfo",
) -> List[Dict]:
    """Auto-detect executable scripts when _scripts.json is absent.

    Scans extra_files (DB skills) or the skill directory (filesystem skills)
    for files with known script extensions and returns a whitelist entry for
    each one, so users don't need to manually create _scripts.json.
    """
    scripts: List[Dict] = []

    # Collect candidate filenames
    candidates: List[str] = []

    # From extra_files (DB skills)
    for fname in extra_files:
        if fname.startswith("_"):
            continue
        ext = Path(fname).suffix.lower()
        if ext in SCRIPT_EXTENSIONS:
            candidates.append(fname)

    # From filesystem (filesystem skills without extra_files)
    if not candidates and not skill_info.is_database and skill_info.content is None:
        skill_dir = skill_info.file_path.parent
        for p in skill_dir.iterdir():
            if (
                p.is_file()
                and not p.name.startswith("_")
                and p.suffix.lower() in SCRIPT_EXTENSIONS
            ):
                candidates.append(p.name)

    # Build whitelist entries
    ext_to_lang = {".py": "python", ".js": "javascript", ".sh": "bash", ".r": "r", ".R": "r"}
    for name in sorted(candidates):
        ext = Path(name).suffix.lower()
        scripts.append(
            {
                "name": name,
                "language": ext_to_lang.get(ext, "python"),
                "timeout": 60,
            }
        )

    return scripts
