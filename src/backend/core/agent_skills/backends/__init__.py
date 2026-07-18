"""Backend abstraction for multi-source skill loading."""

from .protocol import SkillBackendProtocol, SkillFileInfo
from .filesystem import FilesystemBackend
from .composite import CompositeBackend
from .database import DatabaseBackend

__all__ = [
    "SkillBackendProtocol",
    "SkillFileInfo",
    "FilesystemBackend",
    "CompositeBackend",
    "DatabaseBackend",
]
