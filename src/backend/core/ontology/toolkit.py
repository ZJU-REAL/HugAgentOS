"""Toolkit visibility filter driven by a matched ontology workflow."""

from __future__ import annotations

from agentscope.tool import Toolkit


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

    async def _get_available_tools(self, groups=None):  # noqa: ANN001
        tools = await super()._get_available_tools(groups)
        if not self._ontology_hidden_tools:
            return tools
        return {
            name: tool for name, tool in tools.items() if name not in self._ontology_hidden_tools
        }
