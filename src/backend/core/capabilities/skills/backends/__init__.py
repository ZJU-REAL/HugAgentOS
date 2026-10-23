"""Backend abstraction for multi-source skill loading."""

from core.capabilities.skills.backends.protocol import SkillBackendProtocol, SkillFileInfo
from core.capabilities.skills.backends.filesystem import FilesystemBackend
from core.capabilities.skills.backends.composite import CompositeBackend
from core.capabilities.skills.backends.database import DatabaseBackend

__all__ = [
    "SkillBackendProtocol",
    "SkillFileInfo",
    "FilesystemBackend",
    "CompositeBackend",
    "DatabaseBackend",
]
