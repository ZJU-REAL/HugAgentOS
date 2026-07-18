"""Backend protocol for skill storage abstraction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Protocol


@dataclass(frozen=True)
class SkillFileInfo:
    """Information about a skill file from a backend source."""

    skill_id: str  # The skill identifier (folder name or derived from frontmatter)
    file_path: Path  # Absolute path to SKILL.md (sentinel path for DB backend)
    source_name: str  # Backend source name (e.g., "built-in", "user", "project")
    priority: int  # Priority for conflict resolution (higher = higher priority)
    content: Optional[str] = None  # Non-None when content comes from DB (skips file I/O)


class SkillBackendProtocol(Protocol):
    """Protocol for skill storage backends.

    Backends abstract the storage layer for skills, enabling loading from
    filesystems, databases, or remote APIs.
    """

    @property
    def source_name(self) -> str:
        """Human-readable name for this backend source."""
        ...

    @property
    def priority(self) -> int:
        """Priority for conflict resolution (higher wins)."""
        ...

    def list_skill_files(self) -> List[SkillFileInfo]:
        """List all available skill files from this backend.

        Returns:
            List of SkillFileInfo with metadata about each skill file.
        """
        ...

    def read_skill_file(self, skill_id: str) -> str:
        """Read the raw content of a skill file.

        Args:
            skill_id: The skill identifier.

        Returns:
            Raw SKILL.md content as string.

        Raises:
            FileNotFoundError: If skill_id does not exist.
        """
        ...

    def exists(self, skill_id: str) -> bool:
        """Check if a skill exists in this backend.

        Args:
            skill_id: The skill identifier.

        Returns:
            True if skill exists, False otherwise.
        """
        ...
