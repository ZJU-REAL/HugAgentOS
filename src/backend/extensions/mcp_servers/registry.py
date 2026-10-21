"""Built-in MCP installation placement and execution adapters.

Installation is independent of exposure: a cloud-installed gateway may be
available to a hybrid desktop without registering tools on a cloud Agent.
"""

from dataclasses import dataclass
from importlib import import_module
from typing import Literal

Location = Literal["local", "cloud"]
Installation = Literal["local", "cloud", "both"]


@dataclass(frozen=True)
class MCPServer:
    server_id: str
    package: str
    installation: Installation = "both"
    port: int | None = None
    exposure: Literal["process", "desktop_gateway"] = "process"

    def installed_on(self, location: Location) -> bool:
        if location not in ("local", "cloud"):
            raise ValueError("Unknown MCP installation location")
        return self.installation in (location, "both")

    def interface(self):
        return import_module("extensions.mcp_servers." + self.package + ".server")

    def permission_specs(self):
        if self.exposure != "desktop_gateway":
            return {}
        return import_module("extensions.mcp_servers." + self.package + ".permissions").permission_specs()


# Stable ports are part of the deployed MCP contract.
_PROCESS_PORTS = {
    "retrieve_dataset_content": 9100,
    "internet_search": 9102,
    "web_fetch": 9106,
    "automation_task": 9108,
    "skill_manager": 9112,
    "site_publish": 9113,
    "agent_manager": 9115,
    "plugin_manager": 9116,
    "browser_runtime": 9117,
}
SERVERS = {
    key: MCPServer(key, key if key.endswith("_mcp") else key + "_mcp", port=port)
    for key, port in _PROCESS_PORTS.items()
}
SERVERS["hugagent-space-files"] = MCPServer(
    "hugagent-space-files",
    "space_files_mcp",
    installation="cloud",
    exposure="desktop_gateway",
)


def process_servers(location: Location):
    return tuple(
        server
        for server in SERVERS.values()
        if server.exposure == "process" and server.installed_on(location)
    )


def gateway_server(server_id):
    server = SERVERS.get(server_id)
    return (
        server
        if server and server.exposure == "desktop_gateway" and server.installed_on("cloud")
        else None
    )


def gateway_manifests(user):
    if not user or str(user).startswith("api_ak_"):
        return []
    return [
        {
            **server.interface().manifest(),
            "installation": server.installation,
            "exposure": server.exposure,
        }
        for server in SERVERS.values()
        if server.exposure == "desktop_gateway" and server.installed_on("cloud")
    ]


def permission_specs(server_id):
    server = gateway_server(server_id)
    return server.permission_specs() if server else {}
