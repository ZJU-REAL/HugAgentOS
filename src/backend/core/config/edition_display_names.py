"""Community Edition has no commercial capability display metadata."""


def edition_mcp_server_display_names() -> dict[str, str]:
    return {}


def edition_mcp_server_descriptions() -> dict[str, str]:
    return {}


def edition_mcp_user_intros() -> dict[str, str]:
    return {}


def edition_mcp_icons() -> dict[str, str]:
    return {}


def edition_tool_display_names() -> dict[str, str]:
    return {}


__all__ = [
    "edition_mcp_icons",
    "edition_mcp_server_descriptions",
    "edition_mcp_server_display_names",
    "edition_mcp_user_intros",
    "edition_tool_display_names",
]
