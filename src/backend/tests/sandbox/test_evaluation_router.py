"""Every session-scoped operation selects one provider, with no fallback."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.infra.ephemeral import LocalEphemeralState
from core.sandbox import evaluation_binding as binding
from core.sandbox.errors import SandboxError, SandboxConnectError
from core.sandbox.evaluation_provider import EvaluationSandboxProvider
from core.sandbox.protocol import ProcessRequest
from core.sandbox.session_router import SessionSandboxRouter


SESSION = "eval_" + "a" * 32
METHODS = (
    "run_to_completion", "start_process", "write_stdin", "put_file", "get_file",
    "get_file_to_path", "current_sandbox_id", "close_session", "touch_session",
)


def arguments(method, session):
    if method in {"run_to_completion", "start_process"}:
        args = (ProcessRequest(script_content="true", script_name="probe.sh", language="bash",
                               session_id=session, user_id="owner"),)
        return args, ({"yield_time_ms": 25} if method == "start_process" else {})
    if method == "write_stdin":
        return ("process-id",), dict(sandbox_session_id=session, user_id="owner",
                                     chars="", yield_time_ms=25)
    if method == "put_file":
        return (session, "/app/file", b"content"), {"user_id": "owner"}
    if method == "get_file":
        return (session, "/app/file"), {"user_id": "owner"}
    if method == "get_file_to_path":
        return (session, "/app/file", "/tmp/output"), dict(max_bytes=1024, user_id="owner")
    return (session,), {}


@pytest.fixture
def providers(monkeypatch):
    import core.config.settings as config
    import core.sandbox.evaluation_provider as evaluation
    ordinary = SimpleNamespace(name="opensandbox", runs_on_host=False,
                               stage_files=AsyncMock(return_value="personal files"))
    strict = SimpleNamespace()
    for method in METHODS:
        setattr(ordinary, method, AsyncMock(return_value=("ordinary", method)))
        setattr(strict, method, AsyncMock(return_value=("evaluation", method)))
    monkeypatch.setattr(config, "settings", SimpleNamespace(sandbox=SimpleNamespace(provider="opensandbox")))
    monkeypatch.setattr(evaluation, "get_evaluation_provider", lambda: strict)
    return SessionSandboxRouter(ordinary), ordinary, strict


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("session, selected", [(SESSION, "evaluation"), ("chat-normal", "ordinary"), (None, "ordinary")])
async def test_routes_every_operation_without_changing_arguments(providers, method, session, selected):
    router, ordinary, strict = providers
    args, kwargs = arguments(method, session)
    assert await getattr(router, method)(*args, **kwargs) == (selected, method)
    target, untouched = (strict, ordinary) if selected == "evaluation" else (ordinary, strict)
    getattr(target, method).assert_awaited_once_with(*args, **kwargs)
    getattr(untouched, method).assert_not_awaited()


async def test_unscoped_capabilities_still_delegate_to_ordinary_provider(providers):
    router, ordinary, _ = providers
    assert router.name == ordinary.name
    assert router.runs_on_host is False
    assert await router.stage_files("owner", []) == "personal files"
    ordinary.stage_files.assert_awaited_once_with("owner", [])


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("configured", ["script_runner", "cube"])
async def test_evaluation_never_falls_back_to_other_provider(providers, monkeypatch, method, configured):
    import core.config.settings as config
    router, ordinary, strict = providers
    monkeypatch.setattr(config.settings.sandbox, "provider", configured)
    args, kwargs = arguments(method, SESSION)
    with pytest.raises(SandboxError, match="fallback is forbidden"):
        await getattr(router, method)(*args, **kwargs)
    getattr(ordinary, method).assert_not_awaited()
    getattr(strict, method).assert_not_awaited()


async def test_missing_invalid_foreign_and_frozen_bindings_never_reach_normal_provider(providers, monkeypatch):
    import core.sandbox.evaluation_provider as evaluation
    router, ordinary, _ = providers
    state = LocalEphemeralState()
    monkeypatch.setattr(binding, "get_ephemeral_state", lambda: state)
    connect = AsyncMock()
    strict = EvaluationSandboxProvider(connector=connect)
    monkeypatch.setattr(evaluation, "get_evaluation_provider", lambda: strict)
    for session in (SESSION, "eval_malformed"):
        with pytest.raises(SandboxError):
            await router.get_file(session, "/app/file", user_id="owner")
    item = await binding.create("owner", "task-container", 60)
    with pytest.raises(SandboxError, match="owner"):
        await router.put_file(item.session_id, "/app/file", b"x", user_id="intruder")
    await binding.begin_freeze(item.session_id, "owner")
    await binding.finish_freeze(item.session_id, "owner")
    with pytest.raises(SandboxError, match="frozen"):
        await router.get_file(item.session_id, "/app/file", user_id="owner")
    ordinary.get_file.assert_not_awaited()
    ordinary.put_file.assert_not_awaited()
    connect.assert_not_awaited()


async def test_attach_failure_does_not_create_replacement(providers, monkeypatch):
    import core.sandbox.evaluation_provider as evaluation
    router, ordinary, _ = providers
    state = LocalEphemeralState()
    monkeypatch.setattr(binding, "get_ephemeral_state", lambda: state)
    item = await binding.create("owner", "task-container", 60)
    connect = AsyncMock(side_effect=ImportError("SDK unavailable"))
    monkeypatch.setattr(evaluation, "get_evaluation_provider", lambda: EvaluationSandboxProvider(connector=connect))
    with pytest.raises(SandboxConnectError, match="replacement is forbidden"):
        await router.get_file(item.session_id, "/app/file", user_id="owner")
    ordinary.get_file.assert_not_awaited()
    connect.assert_awaited_once_with("task-container")
