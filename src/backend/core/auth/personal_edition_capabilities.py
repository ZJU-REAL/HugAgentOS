"""Community-edition capability defaults."""


def default_capability_layers_for_user(db, user_id: str) -> tuple[()]:
    """CE has no organization-scoped role or team default layers."""
    return ()


def extend_agent_visibility_filters(db, user_id: str, agent_model, filters: list):
    """CE exposes only the caller's personal agents and enabled built-ins."""
    return filters


__all__ = ["default_capability_layers_for_user", "extend_agent_visibility_filters"]
