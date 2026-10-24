"""Run-scoped skill loaders with authorized names and bounded desktop I/O."""
from __future__ import annotations
import asyncio
import logging
import os
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
import frontmatter
from agentscope.skill import LocalSkillLoader, SkillLoaderBase, Skill
from core.capabilities.skills.config import model_facing_skill_dir
logger = logging.getLogger(__name__)

def _parse_runtime_skill(text):
    content = frontmatter.loads(text)
    name, description = content.get("name"), content.get("description")
    if not name or not description:
        return None
    return str(name), str(description), content.content


_cached_runtime_skill = lru_cache(maxsize=256)(_parse_runtime_skill)


class RuntimeNamedSkillLoader(SkillLoaderBase):
    """Read frozen files and expose a deployment-appropriate directory."""

    def __init__(self, directory: str, runtime_name: str, capability_run=None) -> None:
        self.directory = directory
        self.runtime_name = runtime_name
        self.capability_run = capability_run
        self._physical_loader = LocalSkillLoader(directory)

    def _list_desktop_skill(self):
        from core.harness.registry.capability_runtime import validate
        from core.harness.capability_errors import CapabilityError

        # Keep authorization outside the parse-error handling: it must fail the
        # whole enumeration, never quietly omit a revoked or modified skill.
        try:
            validate(self.capability_run, only_skill=self.runtime_name)
        except (OSError, CapabilityError):
            # 被撤销或改动过的单个技能只从枚举里消失，不牵连这一轮其它工具。
            return []
        try:
            entry = Path(self.directory) / "SKILL.md"
            updated_at = entry.stat().st_mtime
            text = entry.read_text(encoding="utf-8")
            parsed = (
                _cached_runtime_skill(text) if len(text) <= 262144 else _parse_runtime_skill(text)
            )
            if parsed is None:
                return []
            _, description, markdown = parsed
            return [
                Skill(
                    name=self.runtime_name,
                    description=description,
                    dir=model_facing_skill_dir(self.runtime_name, self.directory),
                    markdown=markdown,
                    updated_at=updated_at,
                )
            ]
        except Exception as exc:
            logger.warning("Failed to load skill metadata: %s", type(exc).__name__)
            return []

    async def list_skills(self):
        # AgentScope's general filesystem scanner crosses the executor boundary
        # eight times for one known file. Batch that I/O in one worker while
        # preserving its parser, fields and sandbox alias.
        if os.name == "nt" and self.capability_run is not None:
            return await asyncio.to_thread(self._list_desktop_skill)
        from core.harness.registry.capability_runtime import validate

        if self.capability_run is not None:
            # Re-check only this loader's own skill (authorization + fresh hash),
            # not the whole run. N loaders each re-hashing all N skills — twice
            # per assembly — was O(N²) filesystem work and the dominant cost of
            # desktop agent setup; scoping to one skill makes it O(N).
            from core.harness.capability_errors import CapabilityError

            try:
                await asyncio.to_thread(validate, self.capability_run, only_skill=self.runtime_name)
            except (CapabilityError, OSError):
                return []
        physical = await self._physical_loader.list_skills()
        return [
            replace(skill, name=self.runtime_name, dir=model_facing_skill_dir(self.runtime_name, self.directory))
            for skill in physical
        ]


class ParallelRuntimeSkillLoader(SkillLoaderBase):
    """Enumerate independent desktop loaders with bounded I/O concurrency."""

    def __init__(self, loaders):
        self._loaders = tuple(loaders)

    async def list_skills(self):
        limit = asyncio.Semaphore(4)

        async def load(loader):
            async with limit:
                return await loader.list_skills()

        results = await asyncio.gather(
            *(load(loader) for loader in self._loaders), return_exceptions=True
        )
        skills = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            skills.extend(result)
        return skills
