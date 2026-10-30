"""Local→cloud bridge HTTP: pooled client, connect budget, one retry, readable errors."""

from __future__ import annotations

import httpx
import pytest

from core.harness.capability_errors import CloudUnavailable, PackageMissing
from features.desktop.bridge import cloud_http

URL = "https://cloud.example/api/v1/desktop/capability/manifest"


@pytest.fixture
def cloud(monkeypatch):
    """Route the shared client through a handler the test controls."""

    def install(handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(cloud_http, "_client", client)
        return client

    yield install
    monkeypatch.setattr(cloud_http, "_client", None)


def test_handshake_timeout_is_retried_once(cloud):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("_ssl.c:999: The handshake operation timed out", request=request)
        return httpx.Response(200, json={"ok": True})

    cloud(handler)
    assert cloud_http.get(URL).json() == {"ok": True}
    assert len(calls) == 2


def test_repeated_handshake_timeout_reads_as_a_person_would(cloud):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectTimeout("_ssl.c:999: The handshake operation timed out", request=request)

    cloud(handler)
    with pytest.raises(CloudUnavailable) as caught:
        cloud_http.get(URL)
    assert len(calls) == 2
    assert caught.value.message == "连接云端超时，请检查网络或代理设置后重试"
    assert "_ssl" not in cloud_http.describe(caught.value)


def test_read_timeout_is_not_repeated(cloud):
    # The request may already have reached the cloud, so it must not be sent twice.
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("read timed out", request=request)

    cloud(handler)
    with pytest.raises(CloudUnavailable):
        cloud_http.request("PUT", URL, json={"a": 1})
    assert len(calls) == 1


def test_connect_budget_is_configurable(cloud, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(204)

    cloud(handler)
    monkeypatch.setenv(cloud_http.CONNECT_TIMEOUT_ENV, "25")
    cloud_http.get(URL, timeout=60.0)
    assert seen[-1]["connect"] == 25.0
    assert seen[-1]["read"] == 60.0

    for bad in ("abc", "0", "-3"):
        monkeypatch.setenv(cloud_http.CONNECT_TIMEOUT_ENV, bad)
        assert cloud_http.connect_timeout_s() == cloud_http.DEFAULT_CONNECT_TIMEOUT_S
    monkeypatch.delenv(cloud_http.CONNECT_TIMEOUT_ENV)
    assert cloud_http.connect_timeout_s() == cloud_http.DEFAULT_CONNECT_TIMEOUT_S


def test_calls_share_one_pool(monkeypatch):
    monkeypatch.setattr(cloud_http, "_client", None)
    first = cloud_http._shared()
    try:
        assert cloud_http._shared() is first
        first.close()
        assert cloud_http._shared() is not first
    finally:
        cloud_http._shared().close()
        monkeypatch.setattr(cloud_http, "_client", None)


def test_describe_hides_urls_and_transport_internals():
    request = httpx.Request("GET", URL)
    status = httpx.HTTPStatusError(
        "Server error '502 Bad Gateway' for url ...", request=request,
        response=httpx.Response(502, request=request),
    )
    assert cloud_http.describe(status) == "云端暂时不可用（HTTP 502），请稍后重试"
    assert cloud_http.describe(httpx.ConnectError("[Errno 61]", request=request)) == (
        "无法连接云端，请检查网络或代理设置后重试"
    )
    assert cloud_http.describe(PackageMissing("cloud no longer offers this skill")) == (
        "cloud no longer offers this skill"
    )


def test_failed_manifest_sync_stores_the_readable_error(index_db, caps_root, cloud, monkeypatch):
    from features.desktop.bridge import client as bridge_client
    from features.desktop.bridge import client_state as bridge_client_state
    from features.desktop.bridge import identity as bridge_identity
    from tests.features.desktop.capabilities.runtime_recovery_support import state

    st = state("a")
    monkeypatch.setattr(bridge_identity, "get_state", lambda: st)
    monkeypatch.setattr(bridge_client, "refresh_manifest_async", lambda **_: None)
    monkeypatch.setattr(bridge_client_state, "manifest_error", None)

    def handler(request):
        raise httpx.ConnectTimeout("_ssl.c:999: The handshake operation timed out", request=request)

    cloud(handler)
    bridge_client.fetch_manifest_blocking(st)
    assert bridge_client_state.manifest_error == "连接云端超时，请检查网络或代理设置后重试"
