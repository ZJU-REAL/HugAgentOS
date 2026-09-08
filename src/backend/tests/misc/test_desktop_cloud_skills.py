"""桌面双端技能：清单 → 安装意图；同一轮同步即下载发布 → 存储层 revision → 运行视图联接；无镜像拷贝。"""

from __future__ import annotations

import base64
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from core.agent_skills import config as skill_config
from core.capabilities import junction, registry, skills, store
from core.capabilities.paths import KIND_SKILL
from core.capabilities.ref import profile_id
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_skills as cloud_skills
from core.services.desktop_capability_protocol import (
    CapabilityManifestError,
    build_skill_manifest,
    skill_content_hash,
    token_subject,
    validate_skill_manifest,
)


def _skill_md(skill_id: str, body: str = "do it") -> str:
    return f"---\nname: {skill_id}\ndescription: {skill_id} desc\n---\n{body}\n"


def _zip(skill_id: str, files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, data in files.items():
            zf.writestr(f"{skill_id}/{rel}", data)
    return buf.getvalue()


def _entry(skill_id: str, content_hash: str, scope: str = "shared") -> dict:
    return {
        "skill_id": skill_id,
        "display_name": skill_id,
        "description": f"{skill_id} desc",
        "version": "1.0.0",
        "scope": scope,
        "content_hash": content_hash,
        "mcp_server_ids": [],
    }


def _token(user_id: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"u": user_id, "c": "center-" + user_id, "a": 1, "h": "session-" + user_id, "d": "device"}).encode()).decode().rstrip("=")
    return f"dcap2.{body}.sig"


# ── 协议 ─────────────────────────────────────────────────────────────


def test_skill_manifest_roundtrip_and_strictness():
    h = skill_content_hash(_skill_md("a"), {"scripts/run.py": "print(1)"})
    manifest = build_skill_manifest([_entry("a", h)], ["z", "b", "b"])
    assert manifest["suppressed_ids"] == ["b", "z"]
    assert validate_skill_manifest(json.loads(json.dumps(manifest))) == manifest

    bad = json.loads(json.dumps(manifest))
    bad["skills"][0]["description"] = "tampered"
    with pytest.raises(CapabilityManifestError):
        validate_skill_manifest(bad)
    with pytest.raises(CapabilityManifestError):
        validate_skill_manifest({**manifest, "version": 99})
    incomplete = json.loads(json.dumps(manifest))
    del incomplete["skills"][0]["content_hash"]
    with pytest.raises(CapabilityManifestError):
        validate_skill_manifest(incomplete)


def test_skill_content_hash_is_order_independent():
    a = skill_content_hash("x", {"b": "2", "a": "1"})
    b = skill_content_hash("x", {"a": "1", "b": "2"})
    assert a == b and a != skill_content_hash("y", {"a": "1", "b": "2"})


def test_token_subject_and_state_fingerprint_survive_rotation():
    assert token_subject(_token("u-1")) == "u-1"
    assert token_subject("opaque") == ""
    same_user = [
        {"cloud_base": "https://c", "token": _token("u-1")},
        {"cloud_base": "https://c", "token": _token("u-1")},
    ]
    assert bridge._state_fingerprint(same_user[0]) == bridge._state_fingerprint(same_user[1])
    other = {"cloud_base": "https://c", "token": _token("u-2")}
    assert bridge._state_fingerprint(same_user[0]) != bridge._state_fingerprint(other)


# ── 本机侧同步 ─────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, *, content: bytes = b"", json_body=None, etag=""):
        self.status_code = status_code
        self.content = content
        self._json = json_body
        self.headers = {"etag": f'"{etag}"'} if etag else {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeCloud:
    """按 URL 应答 manifest / bundle 的假云端；记录请求以断言 ETag 与下载行为。"""

    def __init__(self, manifest: dict, bundles: dict):
        self.manifest = manifest
        self.bundles = bundles
        self.calls: list = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        if url.endswith("/skills/manifest"):
            if headers.get("If-None-Match") == f'"{self.manifest["revision"]}"':
                return _FakeResponse(304)
            return _FakeResponse(200, json_body={"data": self.manifest})
        sid = url.rsplit("/skills/", 1)[1].split("/")[0]
        if sid not in self.bundles:
            return _FakeResponse(404)
        data, content_hash = self.bundles[sid]
        return _FakeResponse(200, content=data, etag=content_hash)

    def downloads(self) -> list:
        return [u for u, _ in self.calls if u.endswith("/bundle")]


_STATE = {"cloud_base": "https://cloud.example", "token": _token("u-1"), "expires_at": 0}
_PROFILE = profile_id(_STATE["cloud_base"], "u-1")


@pytest.fixture
def dirs(tmp_path, monkeypatch, index_db):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(workspace / "skills"))
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(tmp_path / "caps"))
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "test-secret")
    builtin = tmp_path / "builtin"
    (builtin / "ppt-design").mkdir(parents=True)
    (builtin / "ppt-design" / "SKILL.md").write_text(_skill_md("ppt-design", "old"))
    monkeypatch.setattr(skill_config, "_builtin_skills_dir", lambda: builtin)
    monkeypatch.setattr(skills, "builtin_dir", lambda: builtin)
    bridge.reset_for_tests()
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: True)
    monkeypatch.setattr(bridge, "get_state", lambda: dict(_STATE, expires_at=time.time() + 60))
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "u")
    monkeypatch.setattr("core.agent_skills.cache_refresh.refresh_skill_caches", lambda: None)
    yield tmp_path
    bridge.reset_for_tests()


@pytest.fixture
def index_db(tmp_path, monkeypatch):
    from tests._capability_index import bind_capability_index

    engine, factory = bind_capability_index(tmp_path, monkeypatch)
    yield factory
    engine.dispose()


def _cloud(monkeypatch, skills_: dict, suppressed=()):
    """skills_: {skill_id: {rel: content}} → 假云端 + manifest。"""
    entries, bundles = [], {}
    for sid, files in skills_.items():
        md = files.get("SKILL.md") or _skill_md(sid)
        extra = {k: v for k, v in files.items() if k != "SKILL.md"}
        h = skill_content_hash(md, extra)
        entries.append(_entry(sid, h))
        bundles[sid] = (_zip(sid, {"SKILL.md": md, **extra}), h)
    fake = _FakeCloud(build_skill_manifest(entries, list(suppressed)), bundles)
    monkeypatch.setattr("httpx.get", fake.get)
    return fake


def _iid(sid: str) -> str:
    return registry.install_id(KIND_SKILL, _PROFILE, sid)


def _downloaded_ids(fake) -> list:
    return [url.rsplit("/skills/", 1)[1].split("/")[0] for url in fake.downloads()]


def test_sync_downloads_everything_in_the_manifest(dirs, monkeypatch):
    """登录时的这一次同步就把清单里的技能全部下载好，没有「待下载」这个中间态。"""
    fake = _cloud(monkeypatch, {"market-x": {"scripts/a.py": "print()"}, "ppt-design": {}})
    cloud_skills.sync_blocking(_STATE)

    assert sorted(_downloaded_ids(fake)) == ["market-x", "ppt-design"]
    rows = {i.key: i for i in registry.list_installations(kind=KIND_SKILL, profile_id=_PROFILE)}
    assert set(rows) == {"market-x", "ppt-design"} and all(r.state == "ready" for r in rows.values())
    st = cloud_skills.status()
    assert st["pending_count"] == 0 and st["installed_count"] == 2 and st["profile_id"] == _PROFILE
    # 第二轮：manifest 未变 → 304，且不重复下载
    before = len(fake.calls)
    cloud_skills.sync_blocking(_STATE)
    assert fake.calls[before:][0][1]["If-None-Match"] == f'"{fake.manifest["revision"]}"'
    assert sorted(_downloaded_ids(fake)) == ["market-x", "ppt-design"]


def test_prepare_publishes_revision_and_links_view(dirs, monkeypatch):
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "local-user")
    fake = _cloud(monkeypatch, {"market-x": {"scripts/a.py": "print()"}})
    cloud_skills.sync_blocking(_STATE)
    results = cloud_skills.prepare(_STATE, [_iid("market-x")])
    assert results[0]["ok"] and results[0]["installation"]["state"] == "ready"
    inst = registry.get(_iid("market-x"))
    comp = store.get(KIND_SKILL, _PROFILE, "market-x", inst.resolved_revision)
    assert (comp.path / "scripts" / "a.py").read_text() == "print()"

    view = skill_config.sync_user_skill_view("local-user")
    link = view / "market-x"
    assert junction.is_directory_link(link)
    assert junction.read_directory_link(link) == comp.path.resolve()
    assert (link / "SKILL.md").read_text().startswith("---")
    # 共享目录（设备视图）不含账号私有安装，且没有任何真实拷贝
    shared = skill_config.get_sandbox_skills_dir()
    assert not (shared / "market-x").exists()
    assert junction.is_directory_link(shared / "ppt-design")
    assert len(fake.downloads()) == 1


def test_prepare_rejects_tampered_bundle(dirs, monkeypatch):
    fake = _cloud(monkeypatch, {"market-x": {"SKILL.md": _skill_md("market-x", "real")}})
    fake.bundles["market-x"] = (_zip("market-x", {"SKILL.md": _skill_md("market-x", "evil")}), "x")
    cloud_skills.sync_blocking(_STATE)
    inst = registry.get(_iid("market-x"))
    assert inst.state == "failed" and store.revisions(KIND_SKILL, _PROFILE, "market-x") == []


def test_cloud_copy_wins_over_builtin_and_builtin_returns_when_removed(dirs, monkeypatch):
    _cloud(monkeypatch, {"ppt-design": {"SKILL.md": _skill_md("ppt-design", "cloud")}})
    cloud_skills.sync_blocking(_STATE)
    view = skill_config.sync_user_skill_view("u")
    assert "cloud" in (view / "ppt-design" / "SKILL.md").read_text()
    res = skills.last_resolution("u")
    assert res.reasons["ppt-design"] == "account_unique"
    assert [c.profile for c in res.shadowed["ppt-design"]] == ["builtin"]

    _cloud(monkeypatch, {})
    cloud_skills.sync_blocking(_STATE)
    assert registry.list_installations(kind=KIND_SKILL, profile_id=_PROFILE) == []
    assert len(store.revisions(KIND_SKILL, _PROFILE, "ppt-design")) == 1
    assert "cloud" in store.revisions(KIND_SKILL, _PROFILE, "ppt-design")[0].entry_file.read_text()
    view = skill_config.sync_user_skill_view("u")
    assert "old" in (view / "ppt-design" / "SKILL.md").read_text()


def test_ready_skill_auto_updates_on_new_cloud_content(dirs, monkeypatch):
    _cloud(monkeypatch, {"market-x": {"SKILL.md": _skill_md("market-x", "v1")}})
    cloud_skills.sync_blocking(_STATE)
    r1 = registry.get(_iid("market-x")).resolved_revision

    fake = _cloud(monkeypatch, {"market-x": {"SKILL.md": _skill_md("market-x", "v2")}})
    cloud_skills.sync_blocking(_STATE)
    inst = registry.get(_iid("market-x"))
    assert inst.state == "ready" and inst.resolved_revision != r1
    assert len(fake.downloads()) == 1
    assert {c.revision for c in store.revisions(KIND_SKILL, _PROFILE, "market-x")} == {r1, inst.resolved_revision}
    assert "v1" in store.get(KIND_SKILL, _PROFILE, "market-x", r1).entry_file.read_text()
    view = skill_config.sync_user_skill_view("u")
    assert "v2" in (view / "market-x" / "SKILL.md").read_text()


def test_apply_to_enabled_skill_ids_follows_cloud(dirs, monkeypatch):
    _cloud(monkeypatch, {"ppt-design": {}, "market-x": {}}, suppressed=["word-editing"])
    cloud_skills.sync_blocking(_STATE)
    # 同步即就绪：账号里的技能同一轮全部进清单，云端停用的仍被剔除
    out = bridge.apply_to_enabled_skill_ids(["word-editing", "ppt-design", "local-only"])
    assert out == ["ppt-design", "local-only", "market-x"]
    assert bridge.apply_to_enabled_skill_ids(list(out)) == out
    assert bridge.apply_to_enabled_skill_ids(None) is None


def test_apply_noop_when_bridge_inactive_or_unsynced(dirs, monkeypatch):
    ids = ["ppt-design", "local-only"]
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: False)
    assert bridge.apply_to_enabled_skill_ids(list(ids)) == ids
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: True)
    assert bridge.apply_to_enabled_skill_ids(list(ids)) == ids  # manifest 尚未同步


def test_suppressed_cloud_skill_is_disabled_but_kept(dirs, monkeypatch):
    _cloud(monkeypatch, {"market-x": {}})
    cloud_skills.sync_blocking(_STATE)
    cloud_skills.prepare(_STATE, [_iid("market-x")])
    _cloud(monkeypatch, {}, suppressed=["market-x"])
    cloud_skills.sync_blocking(_STATE)
    inst = registry.get(_iid("market-x"))
    assert inst.state == "ready" and inst.enabled is False
    assert store.revisions(KIND_SKILL, _PROFILE, "market-x")
    assert bridge.apply_to_enabled_skill_ids(["market-x", "other"]) == ["other"]


def test_account_switch_keeps_files_isolated_per_profile(dirs, monkeypatch):
    _cloud(monkeypatch, {"my-private": {"secrets.json": "{}"}})
    cloud_skills.sync_blocking(_STATE)
    cloud_skills.prepare(_STATE, [_iid("my-private")])
    assert store.revisions(KIND_SKILL, _PROFILE, "my-private")

    other = dict(_STATE, token=_token("u-2"))
    monkeypatch.setattr(bridge, "get_state", lambda: dict(other, expires_at=time.time() + 60))
    cloud_skills.on_account_switch()
    _cloud(monkeypatch, {})
    cloud_skills.sync_blocking(other)
    view = skill_config.sync_user_skill_view("u2-local")
    assert not (view / "my-private").exists()
    assert store.revisions(KIND_SKILL, _PROFILE, "my-private")  # A 的文件留在 A 的 profile
    assert cloud_skills.status()["installed_count"] == 0


def test_cloud_source_registered_only_with_store(dirs, monkeypatch):
    names = [s.name for s in skill_config.get_default_skill_sources()]
    assert names[-1] == "cloud" and "user" not in names
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT")
    names = [s.name for s in skill_config.get_default_skill_sources()]
    assert "cloud" not in names and "user" in names


def test_local_fork_takes_the_name_by_preference(dirs, monkeypatch):
    _cloud(monkeypatch, {"ppt-design": {"SKILL.md": _skill_md("ppt-design", "cloud")}})
    cloud_skills.sync_blocking(_STATE)
    cloud_skills.prepare(_STATE, [_iid("ppt-design")])
    md = _skill_md("ppt-design", "mine")
    comp = skills.publish_local_skill(
        "ppt-design", files={"SKILL.md": md}, content_hash=skill_content_hash(md, {}), owner_user_id="u"
    )
    res = skills.resolve_for_user("u")
    assert "ppt-design" in res.conflicts  # cloud + local, both account-level, different content
    registry.set_preference(KIND_SKILL, "ppt-design", comp and registry.install_id(KIND_SKILL, "local", "ppt-design"), chosen_by="u")
    view = skill_config.sync_user_skill_view("u")
    assert "mine" in (view / "ppt-design" / "SKILL.md").read_text()
    assert skills.last_resolution("u").reasons["ppt-design"] == "preference"
