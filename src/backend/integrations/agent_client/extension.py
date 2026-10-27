"""Installed integration contribution: local agent-client session leases."""
NAME = "agent_client"


def setup():
    from core.harness.registry import register_router
    from core.harness.registry.client_sessions import register_client_sessions
    from integrations.agent_client import registry
    from integrations.agent_client.api import router

    register_client_sessions(registry)
    register_router(router)
    return True
