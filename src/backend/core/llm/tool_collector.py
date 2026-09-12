"""ToolCollector — adapts 1.x's incremental ``toolkit.register_*`` to 2.0's one-shot Toolkit.

AgentScope 2.0's ``Toolkit`` is injected once at construction time (``Toolkit(tools=,
mcps=, skills_or_loaders=)``); it has no incremental ``register_tool_function`` /
``register_mcp_client`` / ``register_agent_skill`` methods.

To avoid rewriting the ~15 ``register_*`` in-house tool functions (they all call
``toolkit.register_tool_function(fn, namesake_strategy=...)``), this module provides a
**duck-typed compatible** collector: it exposes methods of the same names but internally
just collects tools into an ``AllowedFunctionTool`` list / skill directory list.
``agent_factory`` passes the collector to every ``register_*``, then constructs once via
``Toolkit(tools=collector.function_tools, mcps=clients,
skills_or_loaders=collector.skill_loaders)``.

Also resolves two behavioral differences in 2.0:
  * ``FunctionTool``'s default ``check_permissions`` returns ASK -> every in-house tool
    would pop HITL. The ``AllowedFunctionTool`` subclass flips the default decision back
    to ALLOW (built-in tools like Bash keep their own dangerous-command checks, since we
    do not touch their check_permissions).
  * Tool functions return ``ToolChunk`` (tool.py already aliases ToolResponse to ToolChunk).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import frontmatter
from typing import Any, Callable, List

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool
from agentscope.skill import LocalSkillLoader, SkillLoaderBase, Skill
from core.llm.tool_permissions import (
    ToolPermissionSpec,
    builtin_tool_permission,
)

logger = logging.getLogger(__name__)


class AllowedFunctionTool(FunctionTool):
    """In-house Python tool: allowed by default (overrides 2.0 FunctionTool's default ASK)."""

    async def check_permissions(self, *args: Any, **kwargs: Any) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="HugAgentOS self-developed tool (auto-allowed).",
        )


def _parse_runtime_skill(text):
    content = frontmatter.loads(text)
    name, description = content.get("name"), content.get("description")
    if not name or not description:
        return None
    return str(name), str(description), content.content


_cached_runtime_skill = lru_cache(maxsize=256)(_parse_runtime_skill)


class RuntimeNamedSkillLoader(SkillLoaderBase):
    """Read frozen physical files while exposing their authorized sandbox alias."""

    def __init__(self, directory: str, runtime_name: str, capability_run=None) -> None:
        self.directory = directory
        self.runtime_name = runtime_name
        self.capability_run = capability_run
        self._physical_loader = LocalSkillLoader(directory)

    def _list_desktop_skill(self):
        from core.capabilities.runtime import validate
        from core.capabilities.errors import CapabilityError

        # Keep authorization outside the parse-error handling: it must fail the
        # whole enumeration, never quietly omit a revoked or modified skill.
        try:
            validate(self.capability_run, only_skill=self.runtime_name)
        except (OSError, CapabilityError):
            if not self.capability_run.allow_unavailable:
                raise
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
                    dir=f"/workspace/skills/{self.runtime_name}",
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
        from core.capabilities.runtime import validate

        if self.capability_run is not None:
            # Re-check only this loader's own skill (authorization + fresh hash),
            # not the whole run. N loaders each re-hashing all N skills — twice
            # per assembly — was O(N²) filesystem work and the dominant cost of
            # desktop agent setup; scoping to one skill makes it O(N).
            from core.capabilities.errors import CapabilityError

            try:
                await asyncio.to_thread(validate, self.capability_run, only_skill=self.runtime_name)
            except (CapabilityError, OSError):
                if not self.capability_run.allow_unavailable:
                    raise
                return []
        physical = await self._physical_loader.list_skills()
        return [
            replace(skill, name=self.runtime_name, dir=f"/workspace/skills/{self.runtime_name}")
            for skill in physical
        ]


class _ParallelRuntimeSkillLoader(SkillLoaderBase):
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


class ToolCollector:
    """Duck-type compatible with the 1.x Toolkit registration interface; actually only collects tools/skills for the 2.0 Toolkit."""

    def __init__(self) -> None:
        # name -> AllowedFunctionTool (deduped by name, supports override/skip)
        self._tools: dict[str, AllowedFunctionTool] = {}
        self._permission_specs: dict[str, ToolPermissionSpec] = {}
        self._tool_order: List[str] = []
        self._skill_loaders: List[Any] = []

    # ── 1.x compatibility interface ─────────────────────────────────────
    def register_tool_function(
        self,
        func: Callable[..., Any],
        *,
        func_description: str | None = None,
        namesake_strategy: str = "override",
        permission: ToolPermissionSpec | None = None,
        **_ignored: Any,
    ) -> None:
        """Collect a tool function as an AllowedFunctionTool.

        namesake_strategy:
          - "override" (default): same name replaces the existing one
          - "skip": same name keeps the existing one and discards the new one
        """
        name = getattr(func, "__name__", None) or "tool"
        if name in self._tools:
            if namesake_strategy == "skip":
                return
            # override: replace, but keep the original ordering position
        permission_spec = permission or builtin_tool_permission(name)
        try:
            ft = AllowedFunctionTool(
                func,
                name=name,
                description=func_description,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tool_collector] 构造 FunctionTool '%s' 失败: %s", name, exc)
            return
        if name not in self._tools:
            self._tool_order.append(name)
        self._tools[name] = ft
        if permission_spec is not None:
            self._permission_specs[name] = permission_spec
        else:
            self._permission_specs.pop(name, None)

    def register_agent_skill(
        self, skill_dir: Any, *, runtime_name: str | None = None, capability_run=None
    ) -> None:
        """Keep physical reads separate from a desktop skill's runtime identity."""
        if not skill_dir:
            return
        if runtime_name is not None:
            if not any(
                isinstance(item, RuntimeNamedSkillLoader)
                and item.directory == skill_dir
                and item.runtime_name == runtime_name
                for item in self._skill_loaders
            ):
                self._skill_loaders.append(
                    RuntimeNamedSkillLoader(skill_dir, runtime_name, capability_run)
                )
        elif skill_dir not in self._skill_loaders:
            self._skill_loaders.append(skill_dir)

    def register_mcp_client(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        # 2.0 goes through Toolkit(mcps=); this should never be called — kept as an empty fallback against legacy call paths.
        logger.warning(
            "[tool_collector] register_mcp_client 被调用但已忽略（2.0 经 Toolkit(mcps=)）。"
        )

    # ── Result accessors for agent_factory ───────────────────────────────
    def get_tool(self, name: str) -> AllowedFunctionTool | None:
        """Get a collected AllowedFunctionTool by name (for tests/introspection; `._func`
        is the original callable, `.input_schema` is the JSON schema)."""
        return self._tools.get(name)

    @property
    def function_tools(self) -> List[AllowedFunctionTool]:
        return [self._tools[n] for n in self._tool_order if n in self._tools]

    @property
    def permission_specs(self) -> dict[str, ToolPermissionSpec]:
        """Detached declaration map consumed by the run-scoped gateway."""
        return dict(self._permission_specs)

    @property
    def skill_loaders(self) -> List[Any]:
        loaders = list(self._skill_loaders)
        if (
            os.name == "nt"
            and len(loaders) >= 8
            and all(isinstance(loader, RuntimeNamedSkillLoader) for loader in loaders)
        ):
            return [_ParallelRuntimeSkillLoader(loaders)]
        return loaders


__all__ = ["AllowedFunctionTool", "ToolCollector"]
