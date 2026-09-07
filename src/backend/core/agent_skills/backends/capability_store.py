"""Skill backend over the desktop capability store (the current cloud account's profile).

Lists installations that are ready and enabled for the bridged account; each
entry points at the immutable revision directory in the store. Nothing here
downloads or decides between same-named skills — the resolver does that in the
composite merge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .filesystem import FilesystemBackend
from .protocol import SkillFileInfo


class CapabilityStoreBackend:
    def __init__(self, priority: int = 150, *, local: bool = False):
        self._priority = priority
        self._local = local

    @property
    def source_name(self) -> str:
        return "device-store" if self._local else "cloud"

    @property
    def priority(self) -> int:
        return self._priority

    def _profile(self) -> Optional[str]:
        from core.capabilities.skills import current_account_profile

        return "local" if self._local else current_account_profile()

    def _installations(self):
        from core.capabilities import registry
        from core.capabilities.paths import KIND_SKILL

        profile = self._profile()
        if not profile:
            return []
        if self._local:
            from core.capabilities.skills import current_local_user_id
            current_user = current_local_user_id()
            return [inst for inst in registry.list_installations(kind=KIND_SKILL, profile_id=profile)
                    if inst.ready and inst.enabled and not inst.payload.get("from_db")
                    and (not inst.payload.get("owner_user_id") or inst.payload["owner_user_id"] == current_user)]
        return [inst for inst in registry.list_installations(kind=KIND_SKILL, profile_id=profile)
                if inst.ready and inst.enabled]

    def change_token(self) -> Tuple[Any, ...]:
        profile = self._profile()
        return tuple(
            (inst.install_id, inst.resolved_revision, inst.enabled, inst.generation)
            for inst in self._installations()
        ) + ((profile,),)

    def _dir_for(self, inst) -> Optional[Path]:
        from core.capabilities import store
        from core.capabilities.paths import KIND_SKILL

        comp = store.get(KIND_SKILL, inst.profile_id, inst.key, inst.resolved_revision or "")
        return comp.path if comp else None

    def list_skill_files(self) -> List[SkillFileInfo]:
        result: List[SkillFileInfo] = []
        for inst in self._installations():
            path = self._dir_for(inst)
            if path is None:
                continue
            result.append(
                SkillFileInfo(
                    skill_id=str(inst.payload.get("runtime_name") or inst.key),
                    file_path=path / "SKILL.md",
                    source_name=self.source_name,
                    priority=self._priority,
                    origin={
                        "install_id": inst.install_id,
                        "profile": inst.profile_id,
                        "revision": inst.resolved_revision,
                        "content_hash": inst.payload.get("resolved_content_hash") or inst.content_hash,
                    },
                )
            )
        return result

    def _find(self, skill_id: str) -> Optional[Path]:
        for inst in self._installations():
            if str(inst.payload.get("runtime_name") or inst.key) == skill_id:
                return self._dir_for(inst)
        return None

    def read_skill_file(self, skill_id: str) -> str:
        path = self._find(skill_id)
        if path is None:
            raise FileNotFoundError(f"cloud skill not installed: {skill_id}")
        return (path / "SKILL.md").read_text(encoding="utf-8")

    def get_extra_files(self, skill_id: str) -> Dict[str, str]:
        path = self._find(skill_id)
        if path is None:
            return {}
        # Same text-file policy as filesystem skills, rooted at the revision dir.
        return FilesystemBackend(path.parent, self.source_name).get_extra_files(path.name)

    def exists(self, skill_id: str) -> bool:
        return self._find(skill_id) is not None
