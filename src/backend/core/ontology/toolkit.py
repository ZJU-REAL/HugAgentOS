"""Toolkit visibility filter driven by a matched ontology workflow."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from agentscope.tool import Toolkit
from jinja2 import Template

from core.llm.execution_manifest import canonical_json, stable_tool_schema_order


@dataclass(frozen=True)
class ExecutionSurfaceSnapshot:
    """One generation shared by manifest construction and model requests."""

    generation: int
    groups: tuple[str, ...]
    _tool_schemas_json: str
    skill_instructions: Optional[str]

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        # AgentScope/provider adapters may normalize dictionaries in place.
        # Decode a detached copy so the content-addressed snapshot cannot drift.
        return json.loads(self._tool_schemas_json)


SurfaceListener = Callable[[ExecutionSurfaceSnapshot], Optional[Awaitable[None]]]


class OntologyFilteredToolkit(Toolkit):
    """Hide forbidden tools from both model schemas and dispatch.

    AgentScope currently exposes filtering through its private
    ``_get_available_tools`` seam.  Keeping the override in this tiny adapter
    localizes that compatibility dependency and gives the L-a gate a second,
    independent line of defense.
    """

    def __init__(self, *args, hidden_tools: set[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._ontology_hidden_tools = set(hidden_tools or ())
        self._execution_surface: Optional[ExecutionSurfaceSnapshot] = None
        self._execution_surface_generation = 0
        self._execution_surface_lock = asyncio.Lock()
        self._execution_surface_listener: Optional[SurfaceListener] = None
        self._capture_surface_skills = False
        self._captured_surface_skills = None

    async def _get_available_tools(self, groups=None):  # noqa: ANN001
        tools = await super()._get_available_tools(groups)
        if not self._ontology_hidden_tools:
            return tools
        return {
            name: tool for name, tool in tools.items() if name not in self._ontology_hidden_tools
        }

    async def _get_available_skills(self, groups=None):  # noqa: ANN001
        """Keep AgentScope's appended skill prompt stable across loader order."""
        skills = await super()._get_available_skills(groups)
        ordered = {name: skills[name] for name in sorted(skills)}
        if self._capture_surface_skills:
            self._captured_surface_skills = ordered
        return ordered

    async def get_tool_schemas(self, groups=None):  # noqa: ANN001
        """Return a detached copy of the run-scoped frozen schema snapshot."""
        snapshot = await self.freeze_execution_surface(groups)
        return snapshot.tool_schemas

    async def get_skill_instructions(self) -> Optional[str]:
        """Return the skill prompt from the same frozen execution surface."""
        snapshot = self._execution_surface
        if snapshot is None:
            snapshot = await self.freeze_execution_surface()
        return snapshot.skill_instructions

    @staticmethod
    def _surface_groups(groups) -> tuple[str, ...]:  # noqa: ANN001
        return tuple(sorted({str(group) for group in (groups or []) if str(group)}))

    async def freeze_execution_surface(
        self, groups=None
    ) -> ExecutionSurfaceSnapshot:  # noqa: ANN001
        """Enumerate tools/skills once and publish an explicit generation.

        ``ManifestBoundAgent`` calls this before AgentScope prepares each model
        input. Repeated calls for the same, non-invalidated group set replay the
        exact snapshot; progressive plugin activation explicitly invalidates it.
        """
        normalized_groups = self._surface_groups(groups)
        async with self._execution_surface_lock:
            current = self._execution_surface
            if current is not None and current.groups == normalized_groups:
                return current

            # AgentScope's tool enumeration already calls
            # ``_get_available_skills`` to decide whether to expose its skill
            # viewer. Capture that exact result and render the skill prompt
            # from it, avoiding a second loader/MCP enumeration.
            self._captured_surface_skills = None
            self._capture_surface_skills = True
            try:
                schemas = stable_tool_schema_order(await super().get_tool_schemas(groups))
            finally:
                self._capture_surface_skills = False
            skills = self._captured_surface_skills
            if skills is None:
                # Compatibility fallback if a future AgentScope version stops
                # consulting skills while constructing the tool surface.
                skills = await self._get_available_skills(groups)
            skill_instructions = None
            if skills:
                skill_instructions = Template(self.skill_instruction_template).render(
                    skills=skills.values(),
                    skill_viewer=self.builtin_skill_viewer.tool.name,
                )
            self._execution_surface_generation += 1
            snapshot = ExecutionSurfaceSnapshot(
                generation=self._execution_surface_generation,
                groups=normalized_groups,
                _tool_schemas_json=canonical_json(schemas),
                skill_instructions=skill_instructions,
            )
            self._execution_surface = snapshot
            listener = self._execution_surface_listener
            if listener is not None:
                result = listener(snapshot)
                if inspect.isawaitable(result):
                    await result
            return snapshot

    def invalidate_execution_surface(self) -> None:
        """Force the next request to publish a new surface generation."""
        self._execution_surface = None

    def set_execution_surface_listener(self, listener: Optional[SurfaceListener]) -> None:
        self._execution_surface_listener = listener


__all__ = ["ExecutionSurfaceSnapshot", "OntologyFilteredToolkit"]
