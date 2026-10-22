"""Behavioral regression coverage."""

from __future__ import annotations
import asyncio
import httpx
import pytest

from tests.sandbox.provider_test_support import _reload_settings_and_factory

pytestmark = pytest.mark.usefixtures("provider_database")


def test_factory_default_is_script_runner(monkeypatch):
    monkeypatch.delenv("SANDBOX_PROVIDER", raising=False)
    f = _reload_settings_and_factory(monkeypatch)
    f.reset_provider_cache()
    p = f.get_sandbox_provider()
    assert p.name == "script_runner"
    assert type(p).__name__ == "SessionSandboxRouter"
    assert type(p._ordinary).__name__ == "ScriptRunnerProvider"
    assert p._for("ordinary-chat") is p._ordinary


def test_factory_unknown_raises(monkeypatch):
    f = _reload_settings_and_factory(monkeypatch, SANDBOX_PROVIDER="nonsense")
    f.reset_provider_cache()
    with pytest.raises(ValueError, match="未知 SANDBOX_PROVIDER"):
        f.get_sandbox_provider()


def test_script_runner_provider_execute_sends_correct_body(monkeypatch):
    f = _reload_settings_and_factory(
        monkeypatch, SANDBOX_PROVIDER="script_runner", SANDBOX_RUNNER_URL="http://test-runner:8900"
    )
    f.reset_provider_cache()

    from core.sandbox import ProcessRequest, get_sandbox_provider

    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "stdout": "hello",
                "stderr": "",
                "exit_code": 0,
                "execution_time_ms": 12,
                "status": "exited",
                "session_id": None,
            }

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json=None):
            captured["url"] = url
            captured["body"] = json
            return FakeResponse()

    monkeypatch.setattr("core.sandbox.providers.script_runner.provider.httpx.AsyncClient", FakeClient)

    p = get_sandbox_provider()._ordinary
    result = asyncio.run(
        p.run_to_completion(
            ProcessRequest(
                script_content="print(1)",
                script_name="exec.py",
                language="python",
                params={"foo": "bar"},
                timeout=30,
                session_id="chat-1",
                user_id="user-1",
            )
        )
    )

    assert captured["url"] == "http://test-runner:8900/processes/start"
    assert captured["body"]["script_content"] == "print(1)"
    assert captured["body"]["script_name"] == "exec.py"
    assert captured["body"]["language"] == "python"
    assert captured["body"]["params"] == {"foo": "bar"}
    assert captured["body"]["timeout"] == 30
    assert captured["body"]["session_id"] == "chat-1"
    assert captured["body"]["user_id"] == "user-1"
    assert result.stdout == "hello"
    assert result.exit_code == 0
    assert result.execution_time_ms == 12


def test_script_runner_provider_never_retries_ambiguous_start(monkeypatch):
    f = _reload_settings_and_factory(monkeypatch, SANDBOX_PROVIDER="script_runner")
    f.reset_provider_cache()

    from core.sandbox import ProcessRequest, SandboxError, get_sandbox_provider

    call_count = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json=None):
            call_count["n"] += 1
            raise httpx.ReadTimeout("simulated")

    monkeypatch.setattr("core.sandbox.providers.script_runner.provider.httpx.AsyncClient", FakeClient)

    p = get_sandbox_provider()._ordinary
    with pytest.raises(SandboxError, match="uncertain"):
        asyncio.run(
            p.run_to_completion(
                ProcessRequest(
                    script_content="x",
                    script_name="x.py",
                    language="python",
                    timeout=10,
                )
            )
        )
    assert call_count["n"] == 1  # never duplicate a potentially running command


def test_script_runner_provider_file_and_lifecycle_calls_are_session_scoped(monkeypatch):
    f = _reload_settings_and_factory(
        monkeypatch,
        SANDBOX_PROVIDER="script_runner",
        SANDBOX_RUNNER_URL="http://test-runner:8900",
    )
    f.reset_provider_cache()

    from core.sandbox import get_sandbox_provider

    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content_b64": "aGVsbG8=", "touched": True}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json=None):
            calls.append((url, json))
            return FakeResponse()

    monkeypatch.setattr("core.sandbox.providers.script_runner.provider.httpx.AsyncClient", FakeClient)

    provider = get_sandbox_provider()._ordinary
    asyncio.run(
        provider.put_file(
            "chat-1",
            "/workspace/report.txt",
            b"hello",
            user_id="user-1",
        )
    )
    assert (
        asyncio.run(
            provider.get_file(
                "chat-1",
                "/workspace/report.txt",
                user_id="user-1",
            )
        )
        == b"hello"
    )
    assert asyncio.run(provider.touch_session("chat-1")) is True
    asyncio.run(provider.close_session("chat-1"))

    assert calls == [
        (
            "http://test-runner:8900/put_file",
            {
                "session_id": "chat-1",
                "user_id": "user-1",
                "path": "/workspace/report.txt",
                "content_b64": "aGVsbG8=",
            },
        ),
        (
            "http://test-runner:8900/get_file",
            {
                "session_id": "chat-1",
                "user_id": "user-1",
                "path": "/workspace/report.txt",
            },
        ),
        ("http://test-runner:8900/sessions/touch", {"session_id": "chat-1"}),
        ("http://test-runner:8900/sessions/close", {"session_id": "chat-1"}),
    ]
    assert asyncio.run(provider.current_sandbox_id("chat-1")) == "script_runner:chat-1"


def test_script_runner_provider_execute_maps_connect_error(monkeypatch):
    f = _reload_settings_and_factory(monkeypatch, SANDBOX_PROVIDER="script_runner")
    f.reset_provider_cache()

    from core.sandbox import ProcessRequest, SandboxConnectError, get_sandbox_provider

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json=None):
            raise httpx.ConnectError("dial tcp: refused")

    monkeypatch.setattr("core.sandbox.providers.script_runner.provider.httpx.AsyncClient", FakeClient)

    p = get_sandbox_provider()._ordinary
    with pytest.raises(SandboxConnectError, match="无法连接"):
        asyncio.run(
            p.run_to_completion(
                ProcessRequest(
                    script_content="x",
                    script_name="x.py",
                    language="python",
                    timeout=10,
                )
            )
        )




def test_explicit_opensandbox_does_not_fall_back_on_missing_dependency(monkeypatch):
    import core.sandbox.providers.edition as edition

    factory = _reload_settings_and_factory(monkeypatch, SANDBOX_PROVIDER="opensandbox")
    def missing_provider():
        raise ModuleNotFoundError("missing SDK dependency")
    monkeypatch.setattr(edition, "PersistentSandboxProvider", missing_provider)
    with pytest.raises(ModuleNotFoundError, match="missing SDK dependency"):
        factory.get_sandbox_provider()

def test_cloud_file_operations_require_direct_mount_provider(monkeypatch):
    from core.sandbox import SandboxError
    factory = _reload_settings_and_factory(
        monkeypatch, SANDBOX_PROVIDER="script_runner", DEPLOY_PROFILE="team")
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)
    provider = factory.get_sandbox_provider()
    with pytest.raises(SandboxError, match="OpenSandbox"):
        asyncio.run(provider.put_file("chat-1", "/workspace/report.txt", b"data", user_id="user-1"))
