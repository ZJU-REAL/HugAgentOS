"""Controlled concurrent responses cannot resurrect revoked cloud intentions."""

import base64
import json
import threading
import httpx
import pytest
from core.capabilities import registry, skills
from core.capabilities.ref import profile_id, cloud_ref
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_skills as cloud_skills
from core.services import desktop_cloud_bundles as bundles
from core.services.desktop_capability_protocol import build_skill_manifest, build_entity_manifest


def state(subject):
    body = base64.urlsafe_b64encode(json.dumps({"u": subject}).encode()).decode().rstrip("=")
    return {
        "cloud_base": "https://concurrency.example/tenant",
        "token": "dcap2." + body + ".synthetic",
    }


def manifest(kind, digest):
    if kind == "skill":
        entries = (
            []
            if digest is None
            else [
                {
                    "skill_id": "item",
                    "display_name": "Item",
                    "description": "",
                    "version": digest[0],
                    "scope": "shared",
                    "content_hash": digest,
                    "mcp_server_ids": [],
                }
            ]
        )
        return build_skill_manifest(entries, [])
    entry = {
        "name": "Item",
        "description": "",
        "version": (digest or "")[0:1],
        "content_hash": digest,
    }
    if kind == "agent":
        entry.update(agent_id="item", is_enabled=True)
    else:
        entry.update(
            install_id="item@user", slug="item", category="", enabled=True, skills=[], mcp=[]
        )
    return build_entity_manifest(kind, [] if digest is None else [entry])


@pytest.mark.parametrize("kind", ["skill", "agent", "plugin"])
@pytest.mark.parametrize("replacement", [None, "b" * 64], ids=["revoked", "new_version"])
def test_late_background_manifest_cannot_replace_new_checkpoint(
    index_db, caps_root, monkeypatch, kind, replacement
):
    st = state(kind + str(replacement))
    profile = profile_id(st["cloud_base"], kind + str(replacement))
    monkeypatch.setattr(bridge, "get_state", lambda: st)
    cloud_skills.on_account_switch()
    bundles.on_account_switch()
    monkeypatch.setattr(skills, "rebuild_views", lambda *_: {})
    monkeypatch.setattr("core.agent_skills.cache_refresh.refresh_skill_caches", lambda: None)
    monkeypatch.setattr(bundles, "_prepare", lambda *_: None)
    original, updated = manifest(kind, "a" * 64), manifest(kind, replacement)
    inst = registry.upsert(
        profile_id=profile,
        ref=cloud_ref(st["cloud_base"], kind, "item", scope="shared"),
        content_hash="a" * 64,
        source="cloud",
    )
    entered, release = threading.Event(), threading.Event()
    failures = []

    def get(url, **kwargs):
        if threading.current_thread().name == "old-manifest":
            entered.set()
            if not release.wait(10):
                raise AssertionError("new checkpoint did not run independently")
            payload = original
        else:
            payload = updated
        return httpx.Response(200, request=httpx.Request("GET", url), json={"data": payload})

    monkeypatch.setattr(httpx, "get", get)

    def background():
        try:
            cloud_skills.sync_blocking(st) if kind == "skill" else bundles.sync_kind(kind, st)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=background, name="old-manifest")
    worker.start()
    try:
        assert entered.wait(5)
        if kind == "skill":
            current = cloud_skills._fetch_manifest(st)
            with bridge.account_scope(st):
                cloud_skills._reconcile_intent(current, st)
        else:
            current = bundles._fetch(kind, st)
            bundles._reconcile(kind, current, st, prepare=False)
        now = registry.get(inst.install_id)
        assert now.state == "removed" if replacement is None else now.content_hash == replacement
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not failures
    final = registry.get(inst.install_id)
    assert final.state == "removed" if replacement is None else final.content_hash == replacement
    cached = cloud_skills._manifest if kind == "skill" else bundles._manifests[kind]
    assert cached["revision"] == updated["revision"]


@pytest.mark.parametrize("kind", ["skill", "agent", "plugin"])
def test_new_failed_request_still_prevents_older_response_resurrection(
    index_db, caps_root, monkeypatch, kind
):
    st = state("failed-" + kind)
    profile = profile_id(st["cloud_base"], "failed-" + kind)
    monkeypatch.setattr(bridge, "get_state", lambda: st)
    cloud_skills.on_account_switch()
    bundles.on_account_switch()
    monkeypatch.setattr(skills, "rebuild_views", lambda *_: {})
    monkeypatch.setattr("core.agent_skills.cache_refresh.refresh_skill_caches", lambda: None)
    monkeypatch.setattr(bundles, "_prepare", lambda *_: None)
    inst = registry.upsert(
        profile_id=profile,
        ref=cloud_ref(st["cloud_base"], kind, "item", scope="shared"),
        content_hash="a" * 64,
    )
    registry.mark_removed(inst.install_id)
    entered, release = threading.Event(), threading.Event()

    def get(url, **kwargs):
        if threading.current_thread().name == "old-failed-manifest":
            entered.set()
            assert release.wait(10)
            return httpx.Response(
                200, request=httpx.Request("GET", url), json={"data": manifest(kind, "a" * 64)}
            )
        return httpx.Response(502, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)

    def background():
        cloud_skills.sync_blocking(st) if kind == "skill" else bundles.sync_kind(kind, st)

    worker = threading.Thread(target=background, name="old-failed-manifest")
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(httpx.HTTPStatusError):
            cloud_skills._fetch_manifest(st) if kind == "skill" else bundles._fetch(kind, st)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert registry.get(inst.install_id).state == "removed"


@pytest.mark.parametrize("kind", ["skill", "agent", "plugin"])
def test_304_gets_new_private_ticket_without_changing_manifest_hash(
    index_db, caps_root, monkeypatch, kind
):
    st = state("cached-" + kind)
    monkeypatch.setattr(bridge, "get_state", lambda: st)
    cloud_skills.on_account_switch()
    bundles.on_account_switch()
    payload = manifest(kind, "a" * 64)
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(
            200 if len(calls) == 1 else 304,
            request=httpx.Request("GET", url),
            json={"data": payload},
        )

    monkeypatch.setattr(httpx, "get", get)
    first = cloud_skills._fetch_manifest(st) if kind == "skill" else bundles._fetch(kind, st)
    if kind == "skill":
        cloud_skills._reconcile_intent(first, st)
    else:
        bundles._reconcile(kind, first, st, prepare=False)
    second = cloud_skills._fetch_manifest(st) if kind == "skill" else bundles._fetch(kind, st)
    assert second._manifest_ticket.sequence > first._manifest_ticket.sequence
    assert json.loads(json.dumps(second)) == payload
    assert calls[1]["headers"]["If-None-Match"] == '"' + payload["revision"] + '"'
    if kind == "skill":
        cloud_skills._reconcile_intent(second, st)
    else:
        bundles._reconcile(kind, second, st, prepare=False)
    from core.capabilities.manifest_order import StaleManifest

    with pytest.raises(StaleManifest):
        if kind == "skill":
            cloud_skills._reconcile_intent(first, st)
        else:
            bundles._reconcile(kind, first, st, prepare=False)
