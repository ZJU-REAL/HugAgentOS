"""Configured-secret canaries; no real configuration, credentials, or network."""

from __future__ import annotations
import asyncio
import io
import json
import zipfile
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.services import desktop_capability as cap
from api.routes.v1 import desktop_capability as routes

CANARY = "synthetic-upstream-secret-20260905"
_REAL_COLLECTOR = cap._known_cloud_secrets


@pytest.fixture(autouse=True)
def synthetic_secrets(monkeypatch):
    monkeypatch.setattr(cap, "_known_cloud_secrets", lambda uid: {CANARY}, raising=False)


@pytest.mark.parametrize(
    "payload",
    [
        {"description": "Example " + CANARY},
        {"inputSchema": {"default": CANARY}},
        {"extra_config": {"custom": CANARY}},
        {"scripts/run.py": "KEY = '" + CANARY + "'"},
        {"content": [{"text": CANARY}]},
        {CANARY: "value"},
    ],
)
def test_nested_known_secret_is_blocked_with_fixed_error(payload):
    with pytest.raises(cap.CapabilityContentRejected) as caught:
        cap.guard_capability_content("user", payload)
    assert CANARY not in str(caught.value)
    assert "configured credentials" in str(caught.value)


def test_unknown_public_text_is_not_rejected_or_rewritten():
    payload = {
        "description": "Use your API key in the local credential manager",
        "default": "normal",
    }
    assert cap.guard_capability_content("user", payload) is payload


def test_zip_scripts_blocked_after_final_archive_is_built():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("skill/scripts/run.py", "TOKEN=" + CANARY)
    with pytest.raises(cap.CapabilityContentRejected):
        cap.guard_capability_bundle("user", (output.getvalue(), "a" * 64))


def test_collector_finds_credentials_but_ignores_public_settings():
    values = cap._secrets_from_config(
        {
            "headers": {"Authorization": "Bearer " + CANARY, "Accept": "application/json"},
            "env": {"UPSTREAM_API_KEY": CANARY, "REGION": "china-east-1", "PATH": "/usr/bin"},
        }
    )
    assert CANARY in values and "Bearer " + CANARY in values
    assert (
        "application/json" not in values
        and "china-east-1" not in values
        and "/usr/bin" not in values
    )


def test_stream_never_emits_a_secret_split_across_chunks():
    async def chunks():
        yield b"safe-prefix:" + CANARY[:12].encode()
        yield CANARY[12:].encode() + b":tail"

    async def collect():
        emitted = []
        with pytest.raises(cap.CapabilityContentRejected):
            async for chunk in cap.guard_capability_stream(chunks(), {CANARY}):
                emitted.append(chunk)
        return b"".join(emitted)

    assert CANARY.encode() not in asyncio.run(collect())


def test_safe_stream_keeps_exact_bytes():
    async def chunks():
        yield b'data: {"normal":'
        yield b'"result"}\n\n'

    async def collect():
        return b"".join([v async for v in cap.guard_capability_stream(chunks(), {CANARY})])

    assert asyncio.run(collect()) == b'data: {"normal":"result"}\n\n'


@pytest.mark.parametrize(
    "path,fn",
    [
        ("/manifest", "build_user_capability_manifest"),
        ("/models", "build_user_model_manifest"),
        ("/skills/manifest", "build_user_skill_manifest"),
        ("/agents/manifest", "build_user_agent_manifest"),
        ("/plugins/manifest", "build_user_plugin_manifest"),
    ],
)
def test_manifest_boundaries_return_fixed_integrity_diagnostic(monkeypatch, path, fn):
    monkeypatch.setattr(cap, fn, lambda uid: {"revision": "a" * 64, "description": CANARY})
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user"
    with TestClient(app) as client:
        response = client.get("/v1/desktop/capability" + path)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "integrity_failed"
    assert CANARY not in response.text


@pytest.mark.parametrize(
    "kind,fn",
    [
        ("skills", "resolve_skill_bundle"),
        ("agents", "resolve_agent_bundle"),
        ("plugins", "resolve_plugin_bundle"),
    ],
)
def test_each_bundle_download_rejects_canary(monkeypatch, kind, fn):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("example/body.txt", CANARY)
    monkeypatch.setattr(cap, fn, lambda uid, key: (output.getvalue(), "a" * 64))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user"
    with TestClient(app) as client:
        response = client.get("/v1/desktop/capability/" + kind + "/example/bundle")
    assert response.status_code == 422 and CANARY not in response.text


def test_json_tool_success_result_is_checked(monkeypatch):
    monkeypatch.setattr(cap, "resolve_gateway_tool", lambda *a, **k: {"user_id": "user"})

    async def invoke(*args):
        return {"content": [{"text": CANARY}]}

    monkeypatch.setattr(cap, "invoke_gateway_tool", invoke)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user"
    with TestClient(app) as client:
        response = client.post(
            "/v1/desktop/capability/gateway/example/call",
            json={"tool_name": "search", "arguments": {}, "schema_hash": "a" * 64},
        )
    assert response.status_code == 422 and response.json()["detail"]["code"] == "integrity_failed"
    assert CANARY not in response.text


@pytest.mark.parametrize("model", [False, True])
def test_both_streaming_gateways_block_cross_chunk_canary(monkeypatch, model):
    import httpx

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"content":"' + CANARY[:10].encode()
            yield CANARY[10:].encode() + b'"}\n\n'

    async def upstream(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

    remote = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(routes, "_gateway_client", remote)
    monkeypatch.setattr(
        cap,
        "resolve_model_gateway_target",
        lambda *a, **k: {
            "url": "https://upstream.example",
            "api_key": CANARY,
            "model_name": "test",
            "provider_type": "chat",
            "path": "chat/completions",
        },
    )
    monkeypatch.setattr(
        cap,
        "resolve_gateway_target",
        lambda *a, **k: {
            "url": "https://upstream.example",
            "headers": {"Authorization": "Bearer " + CANARY},
        },
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user"
    path = "/gateway/models/example/chat/completions" if model else "/gateway/example/mcp"
    with TestClient(app) as client:
        response = client.post("/v1/desktop/capability" + path, json={"messages": []})
    asyncio.run(remote.aclose())
    assert CANARY not in response.text
    assert "integrity_failed" in response.text


def test_collector_reads_only_authorized_mcp_and_model_credentials(monkeypatch):
    monkeypatch.setattr(
        cap,
        "_user_effective_configs",
        lambda *a, **k: (
            ["allowed"],
            {
                "allowed": {
                    "headers": {"X-Private-Token": CANARY},
                    "env": {"API_KEY": "synthetic-env-secret"},
                },
                "denied": {"headers": {"Authorization": "denied-secret"}},
            },
        ),
    )

    class Query:
        def all(self):
            # (provider_id, display_name, model_name, api_key, base_url, extra_config)
            return [
                (
                    "provider-1",
                    "Synthetic",
                    "synthetic-model",
                    "synthetic-model-secret",
                    "https://models.example/v1?key=synthetic-model-url-key",
                    {"headers": {"X-Model-Key": "synthetic-model-header-key"}},
                )
            ]

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def query(self, *args):
            return Query()

    monkeypatch.setattr(cap, "SessionLocal", Database)
    secrets = _REAL_COLLECTOR("user")
    assert (
        CANARY in secrets
        and "synthetic-env-secret" in secrets
        and "synthetic-model-secret" in secrets
    )
    assert "denied-secret" not in secrets
    assert {"synthetic-model-url-key", "synthetic-model-header-key"} <= secrets


def test_collector_unavailability_fails_closed_without_raw_error(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ValueError(CANARY)

    monkeypatch.setattr(cap, "_user_effective_configs", unavailable)
    with pytest.raises(cap.CapabilityContentRejected) as caught:
        _REAL_COLLECTOR("user")
    assert CANARY not in str(caught.value)


async def test_actual_invocation_checks_successful_result(monkeypatch):
    from types import SimpleNamespace
    from core.llm import mcp_pool

    async def fake_tool(**kwargs):
        return SimpleNamespace(
            metadata={}, model_dump=lambda **kwargs: {"content": [{"text": CANARY}]}
        )

    class Client:
        execution_timeout = 1

        async def get_tool(self, name):
            return fake_tool

    monkeypatch.setattr(mcp_pool, "make_client", lambda *args, **kwargs: Client())
    with pytest.raises(cap.CapabilityContentRejected):
        await cap.invoke_gateway_tool(
            {
                "user_id": "user",
                "server_id": "example",
                "target": {"headers": {"Authorization": "Bearer " + CANARY}},
                "tool": {"name": "example", "inputSchema": {"type": "object"}},
            },
            {},
            {},
        )


def test_numeric_limit_fields_named_like_tokens_are_not_secrets():
    """字段名里含 "TOKENS"（数量上限）不是凭据：它的数字值若被当成密钥，会在无关的
    技能 / 智能体 zip 里子串误命中，把每次下载都拦成 integrity_failed。"""
    values = cap._secrets_from_config(
        {
            "env": {
                "QUERY_DATABASE_MAX_OUTPUT_TOKENS": "45000",
                "MAX_TOKENS": "8192",
                "QUERY_DATABASE_RETRY_TIMES": "1",
                "UPSTREAM_API_KEY": CANARY,
            }
        }
    )
    assert CANARY in values
    assert "45000" not in values and "8192" not in values and "1" not in values


def test_custom_key_headers_are_credentials_but_public_headers_are_not():
    values = cap._secrets_from_config(
        {
            "headers": {
                "X-Fixture-Key": CANARY,
                "X-Custom-Key": "synthetic-custom-key",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        }
    )
    assert {CANARY, "synthetic-custom-key"} <= values
    assert "application/json" not in values


def test_url_query_and_userinfo_credentials_include_encoded_and_decoded_forms():
    values = cap._secrets_from_config(
        {
            "url": "https://apiuser:synthetic%2Fpassword@upstream.example/mcp?X-Fixture-Key=synthetic%2Fquery&region=china&signature=synthetic-signature"
        }
    )
    assert {
        "synthetic/password",
        "synthetic%2Fpassword",
        "synthetic/query",
        "synthetic%2Fquery",
        "synthetic-signature",
    } <= values
    assert "china" not in values and "apiuser" not in values


def test_userinfo_token_without_password_is_guarded():
    values = cap._secrets_from_config(
        {"base_url": "https://synthetic-userinfo-token@upstream.example/v1"}
    )
    assert "synthetic-userinfo-token" in values


async def test_actual_invocation_checks_custom_header_echo(monkeypatch):
    from types import SimpleNamespace
    from core.llm import mcp_pool

    monkeypatch.setattr(cap, "_known_cloud_secrets", lambda uid: set())

    async def fake_tool(**kwargs):
        return SimpleNamespace(
            metadata={}, model_dump=lambda **kwargs: {"content": [{"text": CANARY}]}
        )

    class Client:
        execution_timeout = 1

        async def get_tool(self, name):
            return fake_tool

    monkeypatch.setattr(mcp_pool, "make_client", lambda *args, **kwargs: Client())
    with pytest.raises(cap.CapabilityContentRejected):
        await cap.invoke_gateway_tool(
            {
                "user_id": "user",
                "server_id": "example",
                "target": {"headers": {"X-Fixture-Key": CANARY}},
                "tool": {"name": "example", "inputSchema": {"type": "object"}},
            },
            {},
            {},
        )
