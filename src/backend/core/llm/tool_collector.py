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

import logging
from typing import Any, Callable, List

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool

logger = logging.getLogger(__name__)


class AllowedFunctionTool(FunctionTool):
    """In-house Python tool: allowed by default (overrides 2.0 FunctionTool's default ASK)."""

    async def check_permissions(self, *args: Any, **kwargs: Any) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="HugAgentOS self-developed tool (auto-allowed).",
        )


class ToolCollector:
    """Duck-type compatible with the 1.x Toolkit registration interface; actually only collects tools/skills for the 2.0 Toolkit."""

    def __init__(self) -> None:
        # name -> AllowedFunctionTool (deduped by name, supports override/skip)
        self._tools: dict[str, AllowedFunctionTool] = {}
        self._tool_order: List[str] = []
        self._skill_loaders: List[Any] = []

    # ── 1.x compatibility interface ─────────────────────────────────────
    def register_tool_function(
        self,
        func: Callable[..., Any],
        *,
        func_description: str | None = None,
        namesake_strategy: str = "override",
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

    def register_agent_skill(self, skill_dir: Any) -> None:
        """Collect a skill directory (2.0 Toolkit(skills_or_loaders=) accepts str paths)."""
        if skill_dir and skill_dir not in self._skill_loaders:
            self._skill_loaders.append(skill_dir)

    def register_mcp_client(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        # 2.0 goes through Toolkit(mcps=); this should never be called — kept as an empty fallback against legacy call paths.
        logger.warning("[tool_collector] register_mcp_client 被调用但已忽略（2.0 经 Toolkit(mcps=)）。")

    # ── Result accessors for agent_factory ───────────────────────────────
    def get_tool(self, name: str) -> AllowedFunctionTool | None:
        """Get a collected AllowedFunctionTool by name (for tests/introspection; `._func`
        is the original callable, `.input_schema` is the JSON schema)."""
        return self._tools.get(name)

    @property
    def function_tools(self) -> List[AllowedFunctionTool]:
        return [self._tools[n] for n in self._tool_order if n in self._tools]

    @property
    def skill_loaders(self) -> List[Any]:
        return list(self._skill_loaders)


__all__ = ["AllowedFunctionTool", "ToolCollector"]
