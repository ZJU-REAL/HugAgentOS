"""A long sync pass signs with the account's current token; a refused token re-syncs on renewal."""

from __future__ import annotations
from features.desktop.bridge import client as bridge_client
from features.desktop.bridge import client_state as bridge_client_state
from features.desktop.bridge import identity as bridge_identity
from features.desktop.bridge import status as bridge_status

import base64
import json
import threading

import httpx
import pytest
from features.desktop.bridge import client as bridge
from features.desktop.bridge import bundles as desktop_cloud_bundles, skills as desktop_cloud_skills


def _token(user_id: str, nonce: str) -> str:
    claims = {
        "u": user_id,
        "c": "center",
        "a": 1,
        "h": f"session-{user_id}",
        "d": "device",
        "n": nonce,
    }
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"dcap2.{body}.sig"


def _refusal(status: int, state=None) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "GET",
        "https://cloud/api/v1/desktop/capability/plugins/manifest",
        headers={"Authorization": f"Bearer {(state or bridge_identity.get_state())['token']}"},
    )
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


@pytest.fixture
def forced(monkeypatch):
    bridge_identity.reset_for_tests()
    calls = []
    monkeypatch.setattr(bridge_identity, 'purge_persisted_state', lambda: None)
    monkeypatch.setattr(bridge_identity, 'rebuild_identity_views', lambda: None)
    monkeypatch.setattr(bridge_client, 'refresh_manifest_async', lambda **kw: calls.append(kw))
    yield calls
    bridge_identity.reset_for_tests()


def test_headers_sign_with_the_renewed_token_of_the_same_account(forced):
    bridge_client.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    snapshot = bridge_identity.get_state()
    renewed = _token("u-1", "b")
    bridge_client.set_state("https://cloud", renewed, 600, device_id="device")
    assert bridge_identity.cloud_headers(snapshot)["Authorization"] == f"Bearer {renewed}"
    other = {"cloud_base": "https://cloud", "token": _token("u-2", "c"), "device_id": "device"}
    assert bridge_identity.cloud_headers(other)["Authorization"] == f"Bearer {other['token']}"


def test_renewal_after_a_refused_token_resyncs_once(forced):
    bridge_client.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    bridge_client.set_state("https://cloud", _token("u-1", "b"), 600, device_id="device")
    assert len(forced) == 1
    snapshot = bridge_identity.get_state()
    bridge_identity.note_rejected_credential(snapshot, _refusal(500))
    bridge_client.set_state("https://cloud", _token("u-1", "c"), 600, device_id="device")
    assert len(forced) == 1
    # A late refusal of b must not reject the already installed c.
    bridge_identity.note_rejected_credential(snapshot, _refusal(401, snapshot))
    assert bridge_client_state.credential_rejected == ""
    bridge_identity.note_rejected_credential(bridge_identity.get_state(), _refusal(401))
    bridge_client.set_state("https://cloud", _token("u-1", "d"), 600, device_id="device")
    assert len(forced) == 2
    bridge_client.set_state("https://cloud", _token("u-1", "e"), 600, device_id="device")
    assert len(forced) == 2


def test_logout_forgets_the_refusal(forced):
    bridge_client.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    bridge_identity.note_rejected_credential(bridge_identity.get_state(), _refusal(401))
    bridge_identity.clear_state()
    bridge_client.set_state("https://cloud", _token("u-1", "b"), 600, device_id="device")
    assert len(forced) == 2  # first login + re-login only
    bridge_client.set_state("https://cloud", _token("u-1", "c"), 600, device_id="device")
    assert len(forced) == 2


@pytest.mark.parametrize("kind", ["agent", "plugin"])
def test_definition_sync_records_a_refused_credential(forced, monkeypatch, kind):
    bridge_client.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    state = bridge_identity.get_state()
    monkeypatch.setattr(desktop_cloud_bundles, "capabilities_enabled", lambda: True)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            401, request=httpx.Request("GET", url, headers=kw.get("headers"))
        ),
    )
    assert desktop_cloud_bundles.sync_kind(kind, state) is False
    assert bridge_client_state.credential_rejected == bridge_identity.state_fingerprint(state)


def test_skill_sync_records_a_refused_credential(forced, monkeypatch):
    bridge_client.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    state = bridge_identity.get_state()
    monkeypatch.setattr(desktop_cloud_skills, "capabilities_enabled", lambda: True)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            401, request=httpx.Request("GET", url, headers=kw.get("headers"))
        ),
    )
    desktop_cloud_skills.sync_blocking(state)
    assert bridge_client_state.credential_rejected == bridge_identity.state_fingerprint(state)


def test_refusal_of_an_expired_token_is_still_recorded(forced, monkeypatch):
    bridge_client.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    state = bridge_identity.get_state()
    with bridge_client_state.state_lock:
        bridge_client_state.state["expires_at"] = 0  # expired, renewal not pushed yet
    assert bridge_identity.get_state() is None
    monkeypatch.setattr(desktop_cloud_bundles, "capabilities_enabled", lambda: True)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(
            401, request=httpx.Request("GET", url, headers=kw.get("headers"))
        ),
    )
    desktop_cloud_bundles.sync_kind("plugin", state)
    bridge_client.set_state("https://cloud", _token("u-1", "b"), 600, device_id="device")
    assert len(forced) == 2


def test_forced_refresh_during_a_running_pass_reruns_after_it(monkeypatch):
    bridge_identity.reset_for_tests()
    monkeypatch.setattr(bridge_identity, 'purge_persisted_state', lambda: None)
    monkeypatch.setattr(bridge_identity, 'rebuild_identity_views', lambda: None)
    threads, runs = [], []

    class Recorded(threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    monkeypatch.setattr(threading, "Thread", Recorded)

    def headers(st):
        runs.append(st["token"])
        if len(runs) == 1:
            bridge_identity.note_rejected_credential(st, _refusal(401))
            bridge_client.set_state("https://cloud", _token("u-1", "b"), 600, device_id="device")
        raise RuntimeError("cloud unreachable")

    monkeypatch.setattr(bridge_identity, 'cloud_headers', headers)
    bridge_client.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    while any(t.is_alive() for t in list(threads)):
        for t in list(threads):
            t.join(5)
    assert runs == [_token("u-1", "a"), _token("u-1", "b")]
    assert bridge_client_state.refresh_pending is False and bridge_client_state.manifest_fetching is False
    bridge_identity.reset_for_tests()


def test_readiness_reads_progress_without_holding_the_identity_lock(forced, monkeypatch):
    bridge_client.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    seen = {}

    def status():
        def probe():
            seen["lock_free"] = bridge_client_state.state_lock.acquire(timeout=1)
            if seen["lock_free"]:
                bridge_client_state.state_lock.release()

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join()
        return {"revision": "r", "installations": [], "last_error": None}

    monkeypatch.setattr(desktop_cloud_skills, "status", status)
    monkeypatch.setattr(
        desktop_cloud_bundles, "status", lambda: {"agent": status(), "plugin": status()}
    )
    monkeypatch.setattr('features.desktop.capabilities.session_availability.active', lambda _: False)
    assert bridge_status.initial_sync_status()["totals_known"] is True
    assert seen["lock_free"] is True
