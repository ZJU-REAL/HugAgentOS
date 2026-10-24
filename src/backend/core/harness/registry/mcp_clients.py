"""Host-owned SDK client factory used by capability connectivity probes."""
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Any

@dataclass
class ClientFactory:
    create: Callable[..., Any] | None = None

@lru_cache(maxsize=1)
def client_factory() -> ClientFactory:
    return ClientFactory()

def register_mcp_client_factory(factory: Callable[..., Any]) -> None:
    client_factory().create = factory

def discover_tools(*args, **kwargs):
    factory = client_factory().create
    if factory is None:
        raise RuntimeError("MCP client factory is not registered")
    return factory(*args, **kwargs)
