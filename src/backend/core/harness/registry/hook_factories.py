"""Per-run hooks contributed by installed modules."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.harness.hooks import HookSpec

HookFactory = Callable[[Any], Iterable["HookSpec"]]



def register_hook_factory(factory: HookFactory) -> None:
    """Register once; invoke afresh for each agent assembly."""
    if factory not in registration_state().factories:
        registration_state().factories.append(factory)


def hook_specs(request: Any) -> Iterator[HookSpec]:
    for factory in tuple(registration_state().factories):
        yield from factory(request)


# Run-domain policies are created at assembly time, never shared between agents.
from dataclasses import dataclass
from functools import lru_cache


@dataclass
class RunHookFactories:
    steering: Callable[[], Any] | None = None
    command_completion: Callable[[], Iterable[HookSpec]] | None = None


@lru_cache(maxsize=1)
def run_hook_factories() -> RunHookFactories:
    return RunHookFactories()


def register_run_hook_factories(*, steering, command_completion) -> None:
    factories = run_hook_factories()
    factories.steering = steering
    factories.command_completion = command_completion


def create_steering_policy():
    factory = run_hook_factories().steering
    if factory is None:
        raise RuntimeError("Run steering policy is not registered")
    return factory()


def command_completion_hooks():
    factory = run_hook_factories().command_completion
    if factory is None:
        raise RuntimeError("Command completion hooks are not registered")
    return factory()


from dataclasses import dataclass as state_dataclass, field as state_field
from functools import lru_cache as state_cache


@state_dataclass
class RegistrationState:
    """Process-owned registrations; replace only while the host is quiescent."""
    factories: list[HookFactory] = state_field(default_factory=list)


@state_cache(maxsize=1)
def registration_state() -> RegistrationState:
    return RegistrationState()

# Initialize under the import lock before concurrent callers share the owner.
registration_state()
