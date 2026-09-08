"""Batch helper HTTP calls use live credentials with the originating account."""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from api.routes.v1 import internal_batch as batch
from core.capabilities import skills
from core.capabilities.errors import CloudUnavailable
from core.capabilities.ref import profile_id
from core.db.models import BatchPlan, ChatSession, UserShadow
from core.services import desktop_cloud_bridge as bridge
from core.services.model_config import ResolvedModelConfig


def _token(subject="alice", epoch=1, nonce="one"):
    payload = {
        "u": subject,
        "c": "center-" + subject,
        "a": epoch,
        "h": "session-" + str(epoch),
        "d": "device-a",
        "n": nonce,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "dcap2." + encoded + ".test-only"


@pytest.fixture
def state(monkeypatch, caps_root):
    value = {
        "cloud_base": "https://cloud.example",
        "token": _token(),
        "expires_at": 9999999999,
        "device_id": "device-a",
    }
    monkeypatch.setattr(bridge, "get_state", lambda: dict(value) if value else None)
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "local-alice" if value else None)
    config = ResolvedModelConfig(
        base_url=value["cloud_base"] + "/api/v1/desktop/capability/gateway/models/p1",
        api_key="desktop-capability:" + profile_id(value["cloud_base"], "alice"),
        model_name="mock-model",
    )
    monkeypatch.setattr(
        batch.ModelConfigService,
        "get_instance",
        lambda: SimpleNamespace(resolve=lambda role: config),
    )
    return value


def _transport(monkeypatch, handler, *, before_client=None):
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        if before_client:
            before_client()
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(
        batch, "httpx", SimpleNamespace(AsyncClient=factory, HTTPError=httpx.HTTPError)
    )


def _answer(text, status=200):
    return httpx.Response(status, json={"choices": [{"message": {"content": text}}]})


def _sse(text, status=200):
    content = (
        "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\ndata: [DONE]\n\n"
    )
    return httpx.Response(status, text=content, headers={"Content-Type": "text/event-stream"})


@pytest.mark.asyncio
async def test_live_token_and_device_are_injected(state, monkeypatch):
    seen = []

    def upstream(request):
        seen.append(request)
        return _sse("<think>private reasoning</think> result")

    _transport(monkeypatch, upstream)
    assert await batch._call_llm("test input", user_id="local-alice") == "result"
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer " + state["token"]
    assert seen[0].headers["x-desktop-device-id"] == "device-a"
    assert "desktop-capability:" not in str(seen[0].headers)


@pytest.mark.asyncio
async def test_retry_rotates_only_same_session_credentials(state, monkeypatch):
    seen = []
    initial = state["token"]

    def upstream(request):
        seen.append(request)
        if len(seen) == 1:
            state["token"] = _token(nonce="rotated")
            return _sse("", status=400)
        return _sse("result")

    _transport(monkeypatch, upstream)
    assert await batch._call_llm("test input", user_id="local-alice") == "result"
    assert [request.headers["authorization"] for request in seen] == [
        "Bearer " + initial,
        "Bearer " + state["token"],
    ]


@pytest.mark.asyncio
async def test_retry_rejects_relogin_epoch_before_second_http(state, monkeypatch):
    seen = []

    def upstream(request):
        seen.append(request)
        state["token"] = _token(epoch=2)
        return _sse("", status=400)

    _transport(monkeypatch, upstream)
    with pytest.raises(CloudUnavailable):
        await batch._call_llm("test input", user_id="local-alice")
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, "local-bob"])
async def test_other_or_missing_user_never_sends_cloud_prompt(state, monkeypatch, user_id):
    seen = []
    _transport(monkeypatch, lambda request: seen.append(request) or _sse("unexpected"))
    with pytest.raises(CloudUnavailable):
        await batch._call_llm("private input", user_id=user_id)
    assert seen == []


@pytest.mark.asyncio
async def test_logout_after_client_setup_never_sends_prompt(state, monkeypatch):
    seen = []
    _transport(
        monkeypatch,
        lambda request: seen.append(request) or _sse("unexpected"),
        before_client=state.clear,
    )
    with pytest.raises(CloudUnavailable):
        await batch._call_llm("private input", user_id="local-alice")
    assert seen == []


@pytest.mark.asyncio
async def test_regular_cloud_provider_keeps_existing_auth(monkeypatch):
    config = ResolvedModelConfig(
        base_url="https://provider.example/v1",
        api_key="synthetic-provider-key",
        model_name="ordinary",
    )
    monkeypatch.setattr(
        batch.ModelConfigService,
        "get_instance",
        lambda: SimpleNamespace(resolve=lambda role: config),
    )
    seen = []
    _transport(monkeypatch, lambda request: seen.append(request) or _answer("ordinary answer"))
    assert await batch._call_llm("test input") == "ordinary answer"
    assert seen[0].headers["authorization"] == "Bearer synthetic-provider-key"
    assert "x-desktop-device-id" not in seen[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_resolve_route_threads_owner_through_both_real_http_calls(
    state, monkeypatch, db_session, explicit
):
    monkeypatch.setenv("BACKEND_INTERNAL_TOKEN", "test-only-internal")
    monkeypatch.setattr(batch, "SessionLocal", lambda: db_session)
    db_session.add(UserShadow(user_id="local-alice", username="Synthetic Alice"))
    db_session.flush()
    db_session.add(ChatSession(chat_id="test-chat", user_id="local-alice"))
    db_session.commit()
    seen = []

    def upstream(request):
        seen.append(request)
        return _sse('["alpha", "beta"]' if len(seen) == 1 else "请分析对象 {text} 并给出结论。")

    _transport(monkeypatch, upstream)
    app = FastAPI()
    app.include_router(batch.router)
    body = {"instruction": "compare the requested targets", "chat_id": "test-chat"}
    if explicit:
        body["user_id"] = "local-alice"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/internal/batch/resolve",
            json=body,
            headers={"X-Internal-Token": "test-only-internal"},
        )
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert len(seen) == 2
    assert all(request.headers["authorization"] == "Bearer " + state["token"] for request in seen)
    plan = db_session.query(BatchPlan).filter_by(plan_id=response.json()["plan_id"]).one()
    assert plan.user_id == "local-alice"


@pytest.mark.asyncio
async def test_resolve_route_rejects_stale_account_without_writing_plan(
    state, monkeypatch, db_session
):
    monkeypatch.setenv("BACKEND_INTERNAL_TOKEN", "test-only-internal")
    monkeypatch.setattr(batch, "SessionLocal", lambda: db_session)
    seen = []
    _transport(monkeypatch, lambda request: seen.append(request) or _sse("unexpected"))
    app = FastAPI()
    app.include_router(batch.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/internal/batch/resolve",
            json={
                "instruction": "analyze these targets",
                "text_items": ["alpha"],
                "user_id": "local-bob",
            },
            headers={"X-Internal-Token": "test-only-internal"},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "cloud_unavailable"
    assert seen == []
    assert db_session.query(BatchPlan).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream",
    [
        'data: {"choices":[{"delta":{"content":"partial"}}]}\n\ndata: {"error":{"message":"confidential provider detail"}}\n\n',
        "data: invalid-json\n\n",
        'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
    ],
)
async def test_invalid_or_incomplete_stream_never_returns_partial(state, monkeypatch, stream):
    _transport(monkeypatch, lambda request: httpx.Response(200, text=stream))
    with pytest.raises(CloudUnavailable) as error:
        await batch._call_llm("test input", user_id="local-alice")
    assert "confidential provider detail" not in str(error.value)


@pytest.mark.asyncio
async def test_multiple_content_deltas_and_usage_are_assembled(state, monkeypatch):
    chunks = [
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"content": "first "}}]},
        {"choices": [{"delta": {"content": "second"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"completion_tokens": 2}},
    ]
    stream = ": comment\n\n" + "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    stream += "data: [DONE]\n\n"

    def upstream(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=stream)

    _transport(monkeypatch, upstream)
    assert await batch._call_llm("test input", user_id="local-alice") == "first second"


@pytest.mark.asyncio
async def test_response_from_old_epoch_is_not_delivered(state, monkeypatch):
    def upstream(request):
        state["token"] = _token(epoch=2)
        return _sse("private old-account result")

    _transport(monkeypatch, upstream)
    with pytest.raises(CloudUnavailable):
        await batch._call_llm("test input", user_id="local-alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_desktop_auth_and_server_errors_are_not_retried(state, monkeypatch, status):
    seen = []
    _transport(
        monkeypatch,
        lambda request: seen.append(request)
        or httpx.Response(status, json={"error": "synthetic-confidential-provider"}),
    )
    with pytest.raises(CloudUnavailable) as error:
        await batch._call_llm("test input", user_id="local-alice")
    assert len(seen) == 1
    assert "synthetic-confidential-provider" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "integrity_failed",
        "permission_denied",
        "authorization_revoked",
        "invalid_token",
        "scope_denied",
    ],
)
@pytest.mark.parametrize("status", [400, 422])
async def test_security_rejection_never_downgrades_payload(state, monkeypatch, status, code):
    seen = []
    _transport(
        monkeypatch,
        lambda request: seen.append(request)
        or httpx.Response(
            status, json={"detail": {"code": code, "message": "synthetic-private-detail"}}
        ),
    )
    with pytest.raises(CloudUnavailable):
        await batch._call_llm("test input", user_id="local-alice")
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422])
async def test_unknown_extra_field_retains_single_compatibility_retry(state, monkeypatch, status):
    seen = []

    def upstream(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            return httpx.Response(
                status,
                json={
                    "error": {
                        "code": "unsupported_parameter",
                        "message": "extra_body is not supported",
                    }
                },
            )
        return _sse("compatible result")

    _transport(monkeypatch, upstream)
    assert await batch._call_llm("test input", user_id="local-alice") == "compatible result"
    assert len(seen) == 2
    assert "extra_body" in seen[0] and "extra_body" not in seen[1]
    assert all(payload["stream"] is True for payload in seen)
