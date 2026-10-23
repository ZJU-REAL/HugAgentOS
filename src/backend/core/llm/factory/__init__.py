"""Agent construction API. Implementation phases are private to this package."""


def __getattr__(name):
    if name == "create_agent_executor":
        from .build import create_agent_executor

        return create_agent_executor
    raise AttributeError(name)


__all__ = ["create_agent_executor"]
