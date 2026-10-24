"""loader factory responsibilities."""
from __future__ import annotations
from core.capabilities.skills import loader as loader_module_
from typing import Optional
from core.capabilities.skills import loader as loader_module

global_loader: Optional[loader_module_.MultiSourceSkillLoader] = None


def get_skill_loader(reset: bool = False) -> loader_module_.MultiSourceSkillLoader:
    """Get the global MultiSourceSkillLoader instance.

    Args:
        reset: If True, recreate the loader (useful for testing).

    Returns:
        The global MultiSourceSkillLoader instance.
    """
    global global_loader
    if global_loader is None or reset:
        global_loader = loader_module_.MultiSourceSkillLoader()
    return global_loader
