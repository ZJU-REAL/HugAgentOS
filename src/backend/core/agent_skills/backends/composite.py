"""Composite backend for merging multiple skill sources."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from .protocol import SkillBackendProtocol, SkillFileInfo

MergeHook = Callable[
    [Dict[str, List[SkillFileInfo]], Callable[[SkillFileInfo], str]], Dict[str, SkillFileInfo]
]


class CompositeBackend:
    """Combines multiple backends into one id → skill map.

    Without a ``merge`` hook the highest-priority backend wins a same-id clash
    (last one wins on equal priority). The desktop runtime installs a hook that
    hands every multi-candidate id to the capability resolver instead, so the
    decision is explicit, user-visible and never a silent override.
    """

    def __init__(self, backends: List[SkillBackendProtocol], merge: Optional[MergeHook] = None):
        self._backends = backends
        self._merge = merge
        # Pre-compute merged skill map for efficient lookups.
        self._skill_map: Dict[str, SkillFileInfo] = self._merge_skill_files()

    def _merge_skill_files(self) -> Dict[str, SkillFileInfo]:
        if self._merge is not None:
            groups: Dict[str, List[SkillFileInfo]] = {}
            for backend in self._backends:
                for skill_info in backend.list_skill_files():
                    groups.setdefault(skill_info.skill_id, []).append(skill_info)
            # The hook hashes only colliding ids and needs this backend's readers
            # for that; it is handed the bound method rather than the instance
            # because the merge runs inside __init__.
            return self._merge(groups, self.content_hash)

        merged: Dict[str, SkillFileInfo] = {}
        sorted_backends = sorted(self._backends, key=lambda b: b.priority)
        for backend in sorted_backends:
            for skill_info in backend.list_skill_files():
                if (
                    skill_info.skill_id not in merged
                    or skill_info.priority >= merged[skill_info.skill_id].priority
                ):
                    merged[skill_info.skill_id] = skill_info
        return merged

    def content_hash(self, info: SkillFileInfo) -> str:
        """Content hash of one backend entry (used only for colliding ids)."""
        from core.services.desktop_capability_protocol import skill_content_hash

        if info.origin and info.origin.get("content_hash"):
            return str(info.origin["content_hash"])
        owner = next((b for b in self._backends if b.source_name == info.source_name), None)
        if info.is_database:
            content = owner.read_skill_file(info.skill_id) if owner else ""
        elif info.content is not None:
            content = info.content
        else:
            content = info.file_path.read_text(encoding="utf-8")
        extra: Dict[str, str] = {}
        if owner is not None and hasattr(owner, "get_extra_files"):
            extra = owner.get_extra_files(info.skill_id)
        return skill_content_hash(content, extra)

    def change_token(self) -> Tuple[Tuple[str, Any], ...]:
        """Return change tokens from backends that can detect external updates."""
        tokens = []
        for backend in self._backends:
            token_fn = getattr(backend, "change_token", None)
            if callable(token_fn):
                tokens.append((backend.source_name, token_fn()))
        return tuple(tokens)

    def refresh(self) -> None:
        """Refresh the merged skill map from all backends."""
        self._skill_map = self._merge_skill_files()

    @property
    def source_name(self) -> str:
        """Human-readable name for this composite backend."""
        return "composite"

    @property
    def priority(self) -> int:
        """Not applicable for composite backend."""
        return 0

    def list_skill_files(self) -> List[SkillFileInfo]:
        """List all unique skill files after priority-based merging.

        Returns:
            List of SkillFileInfo (one per unique skill_id, highest priority).
        """
        return list(self._skill_map.values())

    def read_skill_file(self, skill_id: str) -> str:
        """Read the raw content of a skill file.

        Args:
            skill_id: The skill identifier.

        Returns:
            Raw SKILL.md content from the highest-priority backend.

        Raises:
            FileNotFoundError: If skill_id does not exist in any backend.
        """
        if skill_id not in self._skill_map:
            raise FileNotFoundError(f"Skill not found in any backend: {skill_id}")

        skill_info = self._skill_map[skill_id]
        if skill_info.is_database:
            for backend in self._backends:
                if backend.source_name == skill_info.source_name:
                    return backend.read_skill_file(skill_id)
            raise FileNotFoundError(f"Owning backend not found for DB skill: {skill_id}")
        # Inline/remote backends may still embed content; filesystem skills use file I/O.
        if skill_info.content is not None:
            return skill_info.content
        return skill_info.file_path.read_text(encoding="utf-8")

    def exists(self, skill_id: str) -> bool:
        """Check if a skill exists in any backend.

        Args:
            skill_id: The skill identifier.

        Returns:
            True if skill exists in any backend, False otherwise.
        """
        return skill_id in self._skill_map

    def get_extra_files(self, skill_id: str) -> dict:
        """Get extra files from the backend that owns this skill.

        Returns:
            {filename: content} dict, or empty dict.
        """
        info = self._skill_map.get(skill_id)
        if info is None:
            return {}
        # A single store backend can contain same-named installations. Read the
        # selected concrete revision, not that backend's first runtime-name hit.
        if info.source_name in ("cloud", "device-store") and not info.is_database:
            from .filesystem import FilesystemBackend
            path = info.file_path.parent
            return FilesystemBackend(path.parent, info.source_name).get_extra_files(path.name)
        # Find the owning backend and delegate
        for backend in self._backends:
            if backend.source_name == info.source_name:
                if hasattr(backend, "get_extra_files"):
                    return backend.get_extra_files(skill_id)
        return {}

    def get_skill_info(self, skill_id: str) -> SkillFileInfo | None:
        """Get the SkillFileInfo for a skill (useful for debugging sources).

        Args:
            skill_id: The skill identifier.

        Returns:
            SkillFileInfo if skill exists, None otherwise.
        """
        return self._skill_map.get(skill_id)
