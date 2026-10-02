"""Explicit and inherited capability selections for one agent assembly."""

from dataclasses import dataclass, field


@dataclass
class RequiredCapabilities:
    connector_ids: list[str] = field(default_factory=list)
    skill_id: str = ""
    skill_name: str = ""
    plugin_id: str = ""
    plugin_name: str = ""
    plugin_skill_ids: list[str] = field(default_factory=list)
    plugin_mcp_ids: list[str] = field(default_factory=list)


@dataclass
class StickyCapabilities:
    direct_mcp_ids: list[str] = field(default_factory=list)
    direct_skill_ids: list[str] = field(default_factory=list)
    plugin_ids: list[str] = field(default_factory=list)
    plugin_mcp_ids: list[str] = field(default_factory=list)
    plugin_skill_ids: list[str] = field(default_factory=list)
