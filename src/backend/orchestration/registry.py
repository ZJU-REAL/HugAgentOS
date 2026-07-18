"""Agent Registry.

Registry specs are intentionally data-oriented so routing/workflow can decide
which runtime profile to use without hardcoding all details in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


AgentFactory = Callable[[], Any]


@dataclass(frozen=True)
class MCPServersSpec:
    """MCP server policy for an agent."""

    enabled: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentSpec:
    """Spec describing how to build and run an agent/subagent."""

    name: str
    factory: Optional[AgentFactory] = None

    # Prompt composition
    prompt_parts: List[str] = field(default_factory=list)

    # Required skills: always loaded (explicit)
    required_skills: List[str] = field(default_factory=list)

    # Available skills: dynamically selected based on user intent (implicit)
    available_skills: List[str] = field(default_factory=list)

    # Tools policy
    tools_allowlist: Optional[List[str]] = None
    mcp_servers: MCPServersSpec = field(default_factory=MCPServersSpec)

    # Runtime controls
    enabled: bool = False
    timeout: int = 60

    # Optional model overrides (string identifiers; resolved by model selector)
    model_name: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

