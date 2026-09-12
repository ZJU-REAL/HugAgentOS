"""A long sync pass signs with the account's current token; a refused token re-syncs on renewal."""

from __future__ import annotations

import base64
import json
import threading

import httpx
import pytest
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_bundles, desktop_cloud_skills


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


def _refusal(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://cloud/api/v1/desktop/capability/plugins/manifest")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )


@pytest.fixture
def forced(monkeypatch):
    bridge.reset_for_tests()
    calls = []
    monkeypatch.setattr(bridge, "_purge_persisted_state", lambda: None)
    monkeypatch.setattr(bridge, "_rebuild_identity_views", lambda: None)
    monkeypatch.setattr(bridge, "_refresh_manifest_async", lambda **kw: calls.append(kw))
    yield calls
    bridge.reset_for_tests()


def test_headers_sign_with_the_renewed_token_of_the_same_account(forced):
    bridge.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    snapshot = bridge.get_state()
    renewed = _token("u-1", "b")
    bridge.set_state("https://cloud", renewed, 600, device_id="device")
    assert bridge.cloud_headers(snapshot)["Authorization"] == f"Bearer {renewed}"
    other = {"cloud_base": "https://cloud", "token": _token("u-2", "c"), "device_id": "device"}
    assert bridge.cloud_headers(other)["Authorization"] == f"Bearer {other['token']}"


def test_renewal_after_a_refused_token_resyncs_once(forced):
    bridge.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    bridge.set_state("https://cloud", _token("u-1", "b"), 600, device_id="device")
    assert len(forced) == 1
    snapshot = bridge.get_state()
    bridge.note_rejected_credential(snapshot, _refusal(500))
    bridge.set_state("https://cloud", _token("u-1", "c"), 600, device_id="device")
    assert len(forced) == 1
    bridge.note_rejected_credential(snapshot, _refusal(401))
    bridge.set_state("https://cloud", _token("u-1", "d"), 600, device_id="device")
    assert len(forced) == 2
    bridge.set_state("https://cloud", _token("u-1", "e"), 600, device_id="device")
    assert len(forced) == 2


def test_logout_forgets_the_refusal(forced):
    bridge.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    bridge.note_rejected_credential(bridge.get_state(), _refusal(401))
    bridge.clear_state()
    bridge.set_state("https://cloud", _token("u-1", "b"), 600, device_id="device")
    assert len(forced) == 2  # first login + re-login only
    bridge.set_state("https://cloud", _token("u-1", "c"), 600, device_id="device")
    assert len(forced) == 2


@pytest.mark.parametrize("kind", ["agent", "plugin"])
def test_definition_sync_records_a_refused_credential(forced, monkeypatch, kind):
    bridge.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    state = bridge.get_state()
    monkeypatch.setattr(desktop_cloud_bundles, "capabilities_enabled", lambda: True)
    monkeypatch.setattr(
        httpx, "get", lambda url, **_: httpx.Response(401, request=httpx.Request("GET", url))
    )
    assert desktop_cloud_bundles.sync_kind(kind, state) is False
    assert bridge._credential_rejected == bridge._state_fingerprint(state)


def test_skill_sync_records_a_refused_credential(forced, monkeypatch):
    bridge.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    state = bridge.get_state()
    monkeypatch.setattr(desktop_cloud_skills, "capabilities_enabled", lambda: True)
    monkeypatch.setattr(
        httpx, "get", lambda url, **_: httpx.Response(401, request=httpx.Request("GET", url))
    )
    desktop_cloud_skills.sync_blocking(state)
    assert bridge._credential_rejected == bridge._state_fingerprint(state)


def test_refusal_of_an_expired_token_is_still_recorded(forced, monkeypatch):
    bridge.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    state = bridge.get_state()
    with bridge._state_lock:
        bridge._state["expires_at"] = 0  # expired, renewal not pushed yet
    assert bridge.get_state() is None
    monkeypatch.setattr(desktop_cloud_bundles, "capabilities_enabled", lambda: True)
    monkeypatch.setattr(
        httpx, "get", lambda url, **_: httpx.Response(401, request=httpx.Request("GET", url))
    )
    desktop_cloud_bundles.sync_kind("plugin", state)
    bridge.set_state("https://cloud", _token("u-1", "b"), 600, device_id="device")
    assert len(forced) == 2


def test_forced_refresh_during_a_running_pass_reruns_after_it(monkeypatch):
    bridge.reset_for_tests()
    monkeypatch.setattr(bridge, "_purge_persisted_state", lambda: None)
    monkeypatch.setattr(bridge, "_rebuild_identity_views", lambda: None)
    threads, runs = [], []

    class Recorded(threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    monkeypatch.setattr(threading, "Thread", Recorded)

    def headers(st):
        runs.append(st["token"])
        if len(runs) == 1:
            bridge.note_rejected_credential(st, _refusal(401))
            bridge.set_state("https://cloud", _token("u-1", "b"), 600, device_id="device")
        raise RuntimeError("cloud unreachable")

    monkeypatch.setattr(bridge, "cloud_headers", headers)
    bridge.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    while any(t.is_alive() for t in list(threads)):
        for t in list(threads):
            t.join(5)
    assert runs == [_token("u-1", "a"), _token("u-1", "b")]
    assert bridge._refresh_pending is False and bridge._manifest_fetching is False
    bridge.reset_for_tests()


def test_readiness_reads_progress_without_holding_the_identity_lock(forced, monkeypatch):
    bridge.set_state("https://cloud", _token("u-1", "a"), 600, device_id="device")
    seen = {}

    def status():
        def probe():
            seen["lock_free"] = bridge._state_lock.acquire(timeout=1)
            if seen["lock_free"]:
                bridge._state_lock.release()

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join()
        return {"revision": "r", "installations": [], "last_error": None}

    monkeypatch.setattr(desktop_cloud_skills, "status", status)
    monkeypatch.setattr(
        desktop_cloud_bundles, "status", lambda: {"agent": status(), "plugin": status()}
    )
    monkeypatch.setattr("core.capabilities.session_availability.active", lambda _: False)
    assert bridge.initial_sync_status()["totals_known"] is True
    assert seen["lock_free"] is True
