"""桌面双端技能同步：协议、落盘/镜像、启用清单合并、账号切换清空。"""

from __future__ import annotations

import base64
import io
import json
import time
import zipfile

import pytest
from core.agent_skills import config as skill_config
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
    body = base64.urlsafe_b64encode(json.dumps({"u": user_id}).encode()).decode().rstrip("=")
    return f"dcap1.{body}.sig"


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
    """按 URL 应答 manifest / bundle 的假云端；记录请求以断言 ETag 行为。"""

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


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "sandbox_skills"))
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "test-secret")
    monkeypatch.setattr(skill_config, "_builtin_skills_dir", lambda: tmp_path / "builtin")
    (tmp_path / "builtin" / "ppt-design").mkdir(parents=True)
    (tmp_path / "builtin" / "ppt-design" / "SKILL.md").write_text(_skill_md("ppt-design", "old"))
    bridge.reset_for_tests()
    monkeypatch.setattr("core.agent_skills.cache_refresh.refresh_skill_caches", lambda: None)
    yield tmp_path
    bridge.reset_for_tests()


def _cloud(monkeypatch, skills: dict, suppressed=()):
    """skills: {skill_id: {rel: content}} → 假云端 + manifest。"""
    entries, bundles = [], {}
    for sid, files in skills.items():
        md = files.get("SKILL.md") or _skill_md(sid)
        extra = {k: v for k, v in files.items() if k != "SKILL.md"}
        h = skill_content_hash(md, extra)
        entries.append(_entry(sid, h))
        bundles[sid] = (_zip(sid, {"SKILL.md": md, **extra}), h)
    fake = _FakeCloud(build_skill_manifest(entries, list(suppressed)), bundles)
    monkeypatch.setattr("httpx.get", fake.get)
    return fake


_STATE = {"cloud_base": "https://cloud.example", "token": _token("u-1"), "expires_at": 0}


def test_sync_installs_mirrors_and_uses_etag(dirs, monkeypatch):
    fake = _cloud(
        monkeypatch,
        {
            "ppt-design": {"SKILL.md": _skill_md("ppt-design", "cloud"), "scripts/a.py": "print()"},
            "my-private": {"secrets.json": '{"k": "v"}'},
        },
        suppressed=["word-editing"],
    )
    cloud_skills.sync_blocking(_STATE)

    root = skill_config.get_cloud_skills_dir()
    shared = skill_config.get_sandbox_skills_dir()
    assert (root / "ppt-design" / "scripts" / "a.py").read_text() == "print()"
    assert "cloud" in (shared / "ppt-design" / "SKILL.md").read_text()
    assert (shared / "my-private" / "secrets.json").exists()
    status = cloud_skills.status()
    assert status["installed_count"] == 2 and status["last_error"] is None

    # 第二轮：manifest 未变 → 304，不再下载任何 bundle
    before = len(fake.calls)
    cloud_skills.sync_blocking(_STATE)
    new_calls = fake.calls[before:]
    assert len(new_calls) == 1 and new_calls[0][0].endswith("/skills/manifest")
    assert new_calls[0][1]["If-None-Match"] == f'"{fake.manifest["revision"]}"'


def test_sync_reasserts_mirror_pruned_at_startup(dirs, monkeypatch):
    _cloud(monkeypatch, {"my-private": {}})
    cloud_skills.sync_blocking(_STATE)
    shared = skill_config.get_sandbox_skills_dir()
    import shutil

    shutil.rmtree(shared / "my-private")
    cloud_skills.sync_blocking(_STATE)  # 304 路径也要把镜像补回来
    assert (shared / "my-private" / "SKILL.md").exists()


def test_removed_cloud_skill_restores_builtin_copy(dirs, monkeypatch):
    _cloud(monkeypatch, {"ppt-design": {"SKILL.md": _skill_md("ppt-design", "cloud")}})
    cloud_skills.sync_blocking(_STATE)
    shared = skill_config.get_sandbox_skills_dir()
    assert "cloud" in (shared / "ppt-design" / "SKILL.md").read_text()

    _cloud(monkeypatch, {})
    cloud_skills.sync_blocking(_STATE)
    assert not (skill_config.get_cloud_skills_dir() / "ppt-design").exists()
    assert "old" in (shared / "ppt-design" / "SKILL.md").read_text()


def test_apply_to_enabled_skill_ids_follows_cloud(dirs, monkeypatch):
    _cloud(monkeypatch, {"ppt-design": {}, "market-x": {}}, suppressed=["word-editing"])
    cloud_skills.sync_blocking(_STATE)
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: True)
    monkeypatch.setattr(bridge, "get_state", lambda: dict(_STATE, expires_at=time.time() + 60))

    out = bridge.apply_to_enabled_skill_ids(["word-editing", "ppt-design", "local-only"])
    assert out == ["local-only", "ppt-design", "market-x"]
    assert bridge.apply_to_enabled_skill_ids(list(out)) == out
    assert bridge.apply_to_enabled_skill_ids(None) is None


def test_apply_noop_when_bridge_inactive_or_unsynced(dirs, monkeypatch):
    ids = ["ppt-design", "local-only"]
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: False)
    assert bridge.apply_to_enabled_skill_ids(list(ids)) == ids
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: True)
    monkeypatch.setattr(bridge, "get_state", lambda: dict(_STATE, expires_at=time.time() + 60))
    assert bridge.apply_to_enabled_skill_ids(list(ids)) == ids  # manifest 尚未同步


def test_failed_bundle_download_is_not_enabled(dirs, monkeypatch):
    fake = _cloud(monkeypatch, {"good": {}, "broken": {}})
    del fake.bundles["broken"]
    cloud_skills.sync_blocking(_STATE)
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: True)
    monkeypatch.setattr(bridge, "get_state", lambda: dict(_STATE, expires_at=time.time() + 60))
    assert bridge.apply_to_enabled_skill_ids(["broken"]) == ["broken", "good"]
    assert cloud_skills.status()["installed_count"] == 1


def test_account_switch_purges_files(dirs, monkeypatch):
    _cloud(monkeypatch, {"my-private": {"secrets.json": "{}"}})
    cloud_skills.sync_blocking(_STATE)
    assert (skill_config.get_cloud_skills_dir() / "my-private").exists()
    cloud_skills.purge_all()
    assert not (skill_config.get_cloud_skills_dir() / "my-private").exists()
    assert not (skill_config.get_sandbox_skills_dir() / "my-private").exists()
    assert cloud_skills.status()["installed_count"] == 0


def test_cloud_source_registered_only_for_bridge_process(dirs, monkeypatch):
    names = [s.name for s in skill_config.get_default_skill_sources()]
    assert names[-1] == "cloud"
    assert skill_config.get_default_skill_sources()[-1].priority > max(
        s.priority for s in skill_config.get_default_skill_sources()[:-1]
    )
    monkeypatch.delenv("HUGAGENT_DESKTOP_BRIDGE_SECRET")
    assert "cloud" not in [s.name for s in skill_config.get_default_skill_sources()]
