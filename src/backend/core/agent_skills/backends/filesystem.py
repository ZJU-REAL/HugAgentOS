"""Filesystem-based skill backend."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from .protocol import SkillBackendProtocol, SkillFileInfo


class FilesystemBackend:
    """Loads skills from a filesystem directory.

    Expected structure:
        root_dir/
            skill-1/
                SKILL.md
            skill-2/
                SKILL.md
    """

    def __init__(
        self,
        root_dir: str | Path,
        source_name: str,
        priority: int = 0,
    ):
        """Initialize filesystem backend.

        Args:
            root_dir: Root directory containing skill folders.
            source_name: Human-readable source name (e.g., "built-in", "user").
            priority: Priority for conflict resolution (higher = higher priority).
        """
        self._root_dir = Path(root_dir).expanduser().resolve()
        self._source_name = source_name
        self._priority = priority

    @property
    def source_name(self) -> str:
        """Human-readable name for this backend source."""
        return self._source_name

    @property
    def priority(self) -> int:
        """Priority for conflict resolution (higher wins)."""
        return self._priority

    def list_skill_files(self) -> List[SkillFileInfo]:
        """List all available skill files from this backend.

        Scans for all SKILL.md files in subdirectories of root_dir.

        Returns:
            List of SkillFileInfo with metadata about each skill file.
        """
        result: List[SkillFileInfo] = []

        if not self._root_dir.exists() or not self._root_dir.is_dir():
            return result

        # Scan for */SKILL.md pattern
        for skill_file in sorted(self._root_dir.glob("*/SKILL.md")):
            if skill_file.is_file():
                skill_id = skill_file.parent.name
                result.append(
                    SkillFileInfo(
                        skill_id=skill_id,
                        file_path=skill_file,
                        source_name=self._source_name,
                        priority=self._priority,
                    )
                )

        return result

    def read_skill_file(self, skill_id: str) -> str:
        """Read the raw content of a skill file.

        Args:
            skill_id: The skill identifier (folder name).

        Returns:
            Raw SKILL.md content as string.

        Raises:
            FileNotFoundError: If skill_id does not exist.
        """
        skill_path = self._root_dir / skill_id / "SKILL.md"
        if not skill_path.exists():
            raise FileNotFoundError(f"Skill not found: {skill_id} at {skill_path}")

        return skill_path.read_text(encoding="utf-8")

    _TEXT_EXTENSIONS = {
        ".md", ".txt", ".json", ".py", ".yaml", ".yml", ".toml", ".cfg",
        ".ini", ".csv", ".xml", ".html", ".css", ".js", ".ts", ".sh", ".conf",
        ".cs", ".csproj", ".sln", ".slnx", ".props", ".targets",
        ".rels",  # OOXML relationship files (used by xlsx/docx templates)
    }

    def get_extra_files(self, skill_id: str) -> dict:
        """Scan skill folder for text files other than SKILL.md.

        Returns:
            {relative_filename: content} dict.
        """
        skill_dir = self._root_dir / skill_id
        if not skill_dir.is_dir():
            return {}
        result: dict = {}
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.name == "SKILL.md":
                continue
            suffix = path.suffix.lower()
            # Handle dotfiles like ".rels" where Python sees empty suffix
            if not suffix and path.name.startswith("."):
                suffix = path.name.lower()
            if suffix not in self._TEXT_EXTENSIONS:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = str(path.relative_to(skill_dir))
            result[rel] = content
        return result

    def exists(self, skill_id: str) -> bool:
        """Check if a skill exists in this backend.

        Args:
            skill_id: The skill identifier (folder name).

        Returns:
            True if skill exists, False otherwise.
        """
        skill_path = self._root_dir / skill_id / "SKILL.md"
        return skill_path.exists() and skill_path.is_file()
