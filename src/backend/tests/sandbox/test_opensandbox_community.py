"""OpenSandbox creation must not load enterprise code."""

import asyncio
import builtins
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider


def test_community_api_session_can_create_without_enterprise_modules(monkeypatch):
    import opensandbox

    original_import = builtins.__import__

    def community_import(name, *args, **kwargs):
        if name == "edition_ee" or name.startswith("edition_ee."):
            raise ModuleNotFoundError("enterprise package absent")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", community_import)
    sandbox = SimpleNamespace(id="community-box")
    create = AsyncMock(return_value=sandbox)
    monkeypatch.setattr(opensandbox.Sandbox, "create", create)
    provider = OpenSandboxProvider.__new__(OpenSandboxProvider)
    provider._make_config = lambda: object()
    provider._image = "community-runtime:test"
    provider._ttl_s = 3600
    provider._repoint_execd_direct = AsyncMock()
    result = asyncio.run(provider._create_jupyter_sandbox(user_id="api_ak_test"))
    assert result is sandbox
    assert create.await_args.kwargs["volumes"] is None
    assert not any("team" in key for key in create.await_args.kwargs["metadata"])


def test_authenticated_controller_renews_and_completes_snapshot_lifecycle(monkeypatch):
    import httpx

    requests = []

    def controller(request):
        assert request.headers["OPEN-SANDBOX-API-KEY"] == "test-controller-key"
        assert "authorization" not in request.headers
        requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/snapshots"):
            return httpx.Response(200, json={"id": "snapshot-1"})
        return httpx.Response(200, json={"status": {"state": "Ready"}})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(controller), **kwargs),
    )
    provider = OpenSandboxProvider.__new__(OpenSandboxProvider)
    provider._domain = "http://controller:8080"
    provider._api_key = "test-controller-key"
    provider._request_timeout_s = 5
    provider._ttl_s = 3600
    provider._http_client = None

    async def lifecycle():
        try:
            await provider._renew(SimpleNamespace(sandbox=SimpleNamespace(id="box-1")))
            assert await provider._take_snapshot("box-1") == "snapshot-1"
            await provider._wait_snapshot_ready("snapshot-1", timeout_s=1)
            await provider._delete_snapshot("snapshot-1")
        finally:
            await provider._http_client.aclose()

    asyncio.run(lifecycle())
    assert requests == [
        ("POST", "/v1/sandboxes/box-1/renew-expiration"),
        ("POST", "/v1/sandboxes/box-1/snapshots"),
        ("GET", "/v1/snapshots/snapshot-1"),
        ("DELETE", "/v1/snapshots/snapshot-1"),
    ]
