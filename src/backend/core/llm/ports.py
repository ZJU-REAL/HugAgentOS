"""Execution-layer services injected into the model runtime by its host."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Protocol


class ToolEffectOutcome(Protocol):
    result: Any
    effect_id: str
    result_id: str
    result_state: str
    invoked: bool


class ToolEffectRuntime(Protocol):
    async def execute_outcome(self, **kwargs: Any) -> ToolEffectOutcome: ...
    def binding_error(self, message: str) -> Exception: ...
    def response_error(self, message: str) -> Exception: ...


def register_tool_effect_runtime(factory: Callable[..., ToolEffectRuntime]) -> None:
    process_state().tool_effect_factory = factory


def tool_effect_runtime(session_factory=None, registry=None) -> ToolEffectRuntime:
    if process_state().tool_effect_factory is None:
        raise RuntimeError("Tool effect execution service is not registered")
    return process_state().tool_effect_factory(session_factory, registry)


class CommandExecution(Protocol):
    async def start(self, provider: Any, request: Any, **kwargs: Any) -> dict: ...
    async def cancel(self, task_id: str, user_id: str, session_id: str) -> Any: ...
    async def wait(self, task_id: str, user_id: str, session_id: str, timeout_ms: int) -> Any: ...
    async def adopt(self, provider: Any, reference: str, **kwargs: Any) -> dict: ...


class CommandRecords(Protocol):
    def resolve_id(self, reference: str, user_id: str, session_id: str) -> str: ...
    def read(self, task_id: str, user_id: str, session_id: str) -> dict: ...
    def list_owned(self, user_id: str, chat_id: str) -> list[dict]: ...


def register_execution_services(*, commands: CommandExecution, records: CommandRecords,
                                parent_snapshot: Callable[..., dict]) -> None:
    process_state().commands, process_state().command_records, process_state().parent_snapshot = commands, records, parent_snapshot


def command_execution() -> CommandExecution:
    if process_state().commands is None:
        raise RuntimeError("Command execution service is not registered")
    return process_state().commands


def command_records() -> CommandRecords:
    if process_state().command_records is None:
        raise RuntimeError("Command record service is not registered")
    return process_state().command_records


def snapshot_parent_runtime(request: Any, **kwargs: Any) -> dict:
    if process_state().parent_snapshot is None:
        raise RuntimeError("Parent runtime snapshot service is not registered")
    return process_state().parent_snapshot(request, **kwargs)


@dataclass
class PortsState:
    tool_effect_factory: Callable[..., ToolEffectRuntime] | None = None
    commands: CommandExecution | None = None
    command_records: CommandRecords | None = None
    parent_snapshot: Callable[..., dict] | None = None


_STATE = PortsState()


def process_state() -> PortsState:
    return _STATE
