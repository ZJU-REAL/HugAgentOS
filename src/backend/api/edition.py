"""Community API providers."""
EE_ROUTERS = ()


def __getattr__(name):
    if name in ("router", "_require_can_add_agent"):
        import api.personal_agents as agents
        return getattr(agents, name)
    raise AttributeError(name)


def gateway_service(db):
    return None
