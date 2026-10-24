"""Reserved execution policy installed by the evaluation host."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol


class EvaluationPolicy(Protocol):
    def apply_context(self, context: dict) -> dict: ...
    def parse_scope(self, **identity: Any) -> Any: ...
    def project_agent(self, agent: Any) -> Any: ...
    def current_paths(self) -> Any: ...
    def register_tools(self, toolkit: Any, scope: Any, **options: Any) -> None: ...
    async def authorize(self, request: Any, user_id: str, *, agent_scoped: bool = False) -> None: ...


def register_evaluation_policy(policy: EvaluationPolicy) -> None:
    pass
    registration_state().policy = policy


def is_evaluation_session(value):
    return isinstance(value, str) and value.startswith("eval_")


def require_policy():
    if registration_state().policy is None:
        raise ValueError("Reserved evaluation execution is not installed")
    return registration_state().policy


def apply_evaluation_scope(context):
    if registration_state().policy is None:
        if any(is_evaluation_session(context.get(k)) for k in ("chat_id", "sandbox_session_id")):
            require_policy()
        return context
    return registration_state().policy.apply_context(context)


def parse_evaluation_scope(**identity):
    if registration_state().policy is None and not any(is_evaluation_session(identity.get(k)) for k in ("chat_id", "session_id")):
        return None
    return require_policy().parse_scope(**identity)


def evaluation_user_agent(agent):
    return require_policy().project_agent(agent)


def current_path_rules():
    return registration_state().policy.current_paths() if registration_state().policy is not None else None


def register_evaluation_tools(toolkit, scope, **options):
    require_policy().register_tools(toolkit, scope, **options)


async def authorize(request, user_id, *, agent_scoped=False):
    if not is_evaluation_session(request.chat_id):
        return
    await require_policy().authorize(request, user_id, agent_scoped=agent_scoped)


@dataclass
class RegistrationState:
    policy: EvaluationPolicy | None = None


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
