from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.sandbox import evaluation_process_guard as guard
from core.sandbox.errors import SandboxError


def program(processes):
    namespace = {"__name__": "test_guard"}
    exec(guard.PROGRAM, namespace)
    killed = []
    namespace["scan"] = lambda: dict(processes)
    def kill(pid, signal):
        killed.append(pid)
        processes.pop(pid, None)
    namespace["os"] = SimpleNamespace(getpid=lambda: 900, kill=kill)
    return namespace, killed


def test_sweep_kills_detached_processes_preserving_baseline_and_ancestors():
    code, killed = program({1: ("1", 0, "S"), 20: ("2", 1, "S"),
                            900: ("3", 20, "R"), 100: ("4", 1, "S")})
    code["sweep"]({"1": "1", "20": "2"})
    assert killed == [100]


def test_reused_pid_is_not_mistaken_for_a_protected_process():
    code, killed = program({1: ("1", 0, "S"), 900: ("3", 1, "R"),
                            100: ("new", 1, "S"), 101: ("z", 1, "Z")})
    code["sweep"]({"1": "1", "100": "old"})
    assert killed == [100]


def test_forking_or_unkillable_process_cannot_be_certified_clean():
    code, _ = program({1: ("1", 0, "S"), 900: ("3", 1, "R"), 100: ("4", 1, "S")})
    code["os"] = SimpleNamespace(getpid=lambda: 900, kill=lambda *args: None)
    with pytest.raises(RuntimeError, match="did not quiesce"):
        code["sweep"]({"1": "1"}, timeout=0.01)


async def test_capture_requires_init_identity_and_uses_isolated_python():
    sandbox = SimpleNamespace(commands=SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(
        exit_code=0, logs=SimpleNamespace(stdout=[SimpleNamespace(text='{"1":"123","20":"456"}')], stderr=[])
    ))))
    assert await guard.capture_baseline(sandbox) == {"1": "123", "20": "456"}
    assert "python3 -I -S" in sandbox.commands.run.call_args.args[0]
    sandbox.commands.run.return_value.logs.stdout[0].text = "{}"
    with pytest.raises(SandboxError):
        await guard.capture_baseline(sandbox)


async def test_clear_failure_is_not_success_and_does_not_echo_payload():
    sandbox = SimpleNamespace(commands=SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(
        exit_code=1, logs=SimpleNamespace(stdout=[], stderr=[SimpleNamespace(text="sensitive")])
    ))))
    with pytest.raises(SandboxError) as exc:
        await guard.clear_processes(sandbox, {"1": "123"})
    assert "sensitive" not in str(exc.value)
    with pytest.raises(SandboxError):
        await guard.clear_processes(sandbox, {})
