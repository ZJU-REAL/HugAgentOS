"""Installed host implementation of local capability management."""
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable


@dataclass
class ManagerRegistration:
    execute: Callable | None = None


@lru_cache(maxsize=1)
def manager_registration() -> ManagerRegistration:
    return ManagerRegistration()


def register_manager_executor(execute: Callable) -> None:
    manager_registration().execute = execute


def execute_manager(*, manager, server_id, headers, cloud_backed, tool_name, input_schema, arguments):
    execute = manager_registration().execute
    if execute is None:
        raise RuntimeError("Local capability manager is not registered")
    return execute(manager=manager, server_id=server_id, headers=headers,
                   cloud_backed=cloud_backed, tool_name=tool_name,
                   input_schema=input_schema, arguments=arguments)
