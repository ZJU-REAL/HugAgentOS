"""Agent construction API. Implementation phases are private to this package."""


def __getattr__(name):
    if name == "create_agent_executor":
        from .build import create_agent_executor

        return create_agent_executor
    if name == "warmup_mcp_tools":
        from .tools.mcp_config import warmup_mcp_tools

        return warmup_mcp_tools
    raise AttributeError(name)


__all__ = ["create_agent_executor", "warmup_mcp_tools"]
