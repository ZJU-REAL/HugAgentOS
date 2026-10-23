"""Authentication and console dependencies keep database work off the event loop."""

import threading

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api import deps
from core.auth import backend
from core.harness.registry import authentication


def _request(method="GET", headers=None):
    raw = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    return Request({"type": "http", "method": method, "path": "/v1/x", "headers": raw, "query_string": b""})


@pytest.fixture
def threads():
    """Record which thread each patched database step ran on."""
    seen = []

    def record(result=None):
        def step(*args, **kwargs):
            seen.append(threading.get_ident())
            return result(*args, **kwargs) if callable(result) else result

        return step

    record.seen = seen
    return record


def _off_loop(threads):
    loop_thread = threading.get_ident()
    assert threads.seen, "the database step never ran"
    assert loop_thread not in threads.seen


def _user(**changes):
    return backend.UserContext(user_id="u1", user_center_id="c1", username="u", **changes)


async def test_api_key_resolution_runs_in_a_worker_thread(monkeypatch, threads):
    monkeypatch.setattr(backend, "_resolve_api_key_context", threads(_user(api_key_id="k")))
    user = await backend.get_current_user(
        _request(headers={"Authorization": "Bearer sk-jx-test"}), None
    )
    assert user.api_key_id == "k"
    _off_loop(threads)


async def test_public_chat_resolution_runs_in_a_worker_thread(monkeypatch, threads):
    from core.auth import public_chat

    monkeypatch.setattr(public_chat, "authenticate_public_chat", threads(_user()))
    for dependency in (backend.get_current_user, backend.require_auth(False)):
        request = _request(headers={"X-Public-Chat": "token"})
        if dependency is backend.get_current_user:
            await dependency(request, None)
        else:
            await dependency(request)
    assert len(threads.seen) == 2
    _off_loop(threads)


async def test_unclaimed_requests_never_reach_the_host_resolver(monkeypatch, threads):
    calls = []

    async def counting_run_in_session(fn, *args, **kwargs):
        calls.append(fn)
        return fn(None, *args, **kwargs)

    monkeypatch.setattr(backend, "run_in_session", counting_run_in_session)
    state = authentication.registration_state()
    monkeypatch.setattr(state, "claims", None)
    monkeypatch.setattr(state, "resolver", None)
    assert await backend._resolve_host_user(_request()) is None

    monkeypatch.setattr(state, "claims", lambda request: False)
    monkeypatch.setattr(state, "resolver", threads(_user()))
    assert await backend._resolve_host_user(_request()) is None
    assert calls == [] and threads.seen == []


async def test_a_claimed_request_is_resolved_in_a_worker_thread(monkeypatch, threads):
    state = authentication.registration_state()
    monkeypatch.setattr(state, "claims", lambda request: request.headers.get("x-host") == "1")
    monkeypatch.setattr(state, "resolver", threads(_user()))
    assert (await backend._resolve_host_user(_request(headers={"X-Host": "1"}))).user_id == "u1"
    _off_loop(threads)


def test_desktop_bridge_claims_only_requests_with_its_credential(monkeypatch):
    from features.desktop.auth import bridge

    monkeypatch.delenv(bridge.BRIDGE_SECRET_ENV, raising=False)
    assert not bridge.claims_request(_request(headers={bridge.BRIDGE_SECRET_HEADER: "s"}))
    monkeypatch.setenv(bridge.BRIDGE_SECRET_ENV, "s")
    assert not bridge.claims_request(_request())
    assert bridge.claims_request(_request(headers={bridge.BRIDGE_SECRET_HEADER: "s"}))


class _Audit:
    def __init__(self, seen):
        self.seen = seen

    def __call__(self, db):
        return self

    def log_denial(self, **kwargs):
        self.seen.append(threading.get_ident())


async def test_console_token_grant_is_audited_in_a_worker_thread(monkeypatch, threads):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(deps, "_audit_grant", threads())
    await deps.require_admin(
        _request("POST", {"Authorization": "Bearer admin-token"}), "Bearer admin-token"
    )
    _off_loop(threads)


async def test_console_session_flag_and_denial_run_in_a_worker_thread(monkeypatch, threads):
    monkeypatch.setenv("CONFIG_TOKEN", "config-token")

    async def session_user(request):
        return "u1"

    monkeypatch.setattr(deps, "_resolve_session_user_id", session_user)
    monkeypatch.setattr(deps, "_user_has_meta_flag", threads(False))
    monkeypatch.setattr(deps, "AuditLogRepository", _Audit(threads.seen))
    with pytest.raises(HTTPException) as error:
        await deps.require_config(_request(headers={"Authorization": "Bearer wrong"}), "Bearer wrong")
    assert error.value.status_code == 401
    assert len(threads.seen) == 2  # flag check + denial audit
    _off_loop(threads)


async def test_prompt_snapshot_gate_audits_denial_in_a_worker_thread(monkeypatch, threads):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(deps, "AuditLogRepository", _Audit(threads.seen))
    with pytest.raises(HTTPException):
        await deps.require_admin_or_config(_request(), "Bearer wrong")
    _off_loop(threads)


async def test_delegated_gates_check_capabilities_in_a_worker_thread(monkeypatch, threads):
    monkeypatch.setenv("CONFIG_TOKEN", "")
    monkeypatch.setenv("ADMIN_TOKEN", "")
    monkeypatch.setattr(deps, "user_can_manage_system_settings", threads(True))
    monkeypatch.setattr(deps, "user_can_manage_ontology_governance", threads(True))
    monkeypatch.setattr(deps, "_audit_grant", threads())
    assert await deps.require_system_settings(_request(), _user()) == "u1"
    assert await deps.require_ontology_governance(_request(), _user()) == "u1"
    _off_loop(threads)


async def test_delegated_gate_denies_in_a_worker_thread(monkeypatch, threads):
    monkeypatch.setenv("CONFIG_TOKEN", "")
    monkeypatch.setattr(deps, "user_can_manage_system_settings", threads(False))
    monkeypatch.setattr(deps, "AuditLogRepository", _Audit(threads.seen))
    with pytest.raises(HTTPException) as error:
        await deps.require_system_settings(_request(), _user())
    assert error.value.status_code == 403
    _off_loop(threads)


async def test_super_admin_gate_runs_in_a_worker_thread(monkeypatch, threads):
    monkeypatch.setenv("ADMIN_TOKEN", "")
    monkeypatch.setattr(deps, "AuditLogRepository", _Audit(threads.seen))
    with pytest.raises(HTTPException) as error:
        await deps.require_super_admin(_request())
    assert error.value.status_code == 401
    _off_loop(threads)
