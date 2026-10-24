"""skill paths responsibilities."""
from __future__ import annotations
from pathlib import Path
from typing import List, Optional
from extensions.paths import SKILLS_DIR
from features.desktop.capabilities.paths import capability_root

def builtin_dir() -> Path:
    return SKILLS_DIR / "default"


def allowed_link_roots() -> List[Path]:
    roots = [builtin_dir()]
    root = capability_root()
    if root is not None:
        roots.insert(0, root)
    return roots


def device_view_dir() -> Path:
    from core.capabilities.skills.config import get_sandbox_skills_dir

    return get_sandbox_skills_dir()


def user_view_dir(user_id: Optional[str]) -> Optional[Path]:
    from core.capabilities.skills.config import get_user_skills_dir

    return get_user_skills_dir(user_id)
