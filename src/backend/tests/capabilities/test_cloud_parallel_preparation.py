"""Cold downloads overlap without losing results or refreshing each package view."""
import threading
from core.capabilities import skills
from core.services import desktop_cloud_skills as cloud_skills


def test_prepare_overlaps_four_downloads_and_preserves_failures(monkeypatch):
    barrier = threading.Barrier(4, timeout=5)
    lock = threading.Lock()
    active = 0
    peak = 0
    refreshed = []

    def prepare_one(state, iid):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            # Two complete waves: a serial implementation cannot pass this barrier.
            barrier.wait()
            assert state == {"account": "same-account"}
            if iid == "3":
                raise ValueError("bad package")
            return {"id": iid}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(cloud_skills, "prepare_one", prepare_one)
    monkeypatch.setattr(skills, "bump_view_generation", lambda: None)
    monkeypatch.setattr(skills, "rebuild_views", lambda owner: refreshed.append(owner))
    monkeypatch.setattr("core.agent_skills.cache_refresh.refresh_skill_caches", lambda: None)
    result = cloud_skills.prepare({"account": "same-account"}, [str(i) for i in range(8)])
    assert peak == 4
    assert [row["install_id"] for row in result] == [str(i) for i in range(8)]
    assert [row["install_id"] for row in result if not row["ok"]] == ["3"]
    assert refreshed == [None]


def test_sync_does_not_repeat_the_completed_batch_view_refresh(monkeypatch):
    from contextlib import nullcontext
    from core.services import desktop_cloud_bridge as bridge
    from core.capabilities import manifest_order
    manifest = {"revision": "new", "skills": [{}]}
    monkeypatch.setattr(cloud_skills, "capabilities_enabled", lambda: True)
    monkeypatch.setattr(cloud_skills, "_manifest", None)
    monkeypatch.setattr(cloud_skills, "_fetch_manifest", lambda _: manifest)
    monkeypatch.setattr(cloud_skills, "_profile", lambda _: "profile")
    monkeypatch.setattr(bridge, "account_scope", lambda _: nullcontext())
    monkeypatch.setattr(manifest_order, "apply", lambda *_: nullcontext())
    monkeypatch.setattr(cloud_skills, "_reconcile_intent", lambda *_: ["id"])
    monkeypatch.setattr(cloud_skills, "prepare", lambda *_: [{"ok": True}])
    def unexpected_refresh(*_):
        raise AssertionError("prepare already refreshed the completed batch")
    monkeypatch.setattr(skills, "rebuild_views", unexpected_refresh)
    cloud_skills.sync_blocking({})


def test_failed_batch_still_refreshes_manifest_revocations(monkeypatch):
    from contextlib import nullcontext
    from core.services import desktop_cloud_bridge as bridge
    from core.capabilities import manifest_order
    refreshed = []
    manifest = {"revision": "new", "skills": [{}]}
    monkeypatch.setattr(cloud_skills, "capabilities_enabled", lambda: True)
    monkeypatch.setattr(cloud_skills, "_manifest", None)
    monkeypatch.setattr(cloud_skills, "_fetch_manifest", lambda _: manifest)
    monkeypatch.setattr(cloud_skills, "_profile", lambda _: "profile")
    monkeypatch.setattr(bridge, "account_scope", lambda _: nullcontext())
    monkeypatch.setattr(manifest_order, "apply", lambda *_: nullcontext())
    monkeypatch.setattr(cloud_skills, "_reconcile_intent", lambda *_: ["id"])
    monkeypatch.setattr(cloud_skills, "prepare", lambda *_: [{"ok": False}])
    monkeypatch.setattr(skills, "bump_view_generation", lambda: None)
    monkeypatch.setattr(skills, "rebuild_views", lambda *_: refreshed.append(True))
    monkeypatch.setattr("core.agent_skills.cache_refresh.refresh_skill_caches", lambda: None)
    cloud_skills.sync_blocking({})
    assert refreshed == [True]
