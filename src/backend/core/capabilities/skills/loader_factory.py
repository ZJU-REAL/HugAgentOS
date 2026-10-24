"""The process-wide skill loader instance."""
from __future__ import annotations
from core.capabilities.skills import loader
from typing import Optional

global_loader: Optional[loader.MultiSourceSkillLoader] = None


def get_skill_loader(reset: bool = False) -> loader.MultiSourceSkillLoader:
    """Get the global MultiSourceSkillLoader instance.

    Args:
        reset: If True, recreate the loader (useful for testing).

    Returns:
        The global MultiSourceSkillLoader instance.
    """
    global global_loader
    if global_loader is None or reset:
        global_loader = loader.MultiSourceSkillLoader()
    return global_loader
