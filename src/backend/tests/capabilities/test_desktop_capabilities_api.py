"""本机能力接口：登录同步即全部就绪、只读的来源清单、视图重建。"""

from __future__ import annotations

import base64
import io
import json
import time
import zipfile

import pytest
from api.routes.v1.desktop_capabilities import router
from core.agent_skills import config as skill_config
from core.auth.backend import UserContext, get_current_user
from core.capabilities import junction, registry, skills, store
from core.capabilities.paths import KIND_SKILL
from core.capabilities.ref import cloud_ref, profile_id
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_skills as cloud_skills
from core.services.desktop_capability_protocol import (
    build_entity_manifest,
    build_skill_manifest,
    skill_content_hash,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

USER = "local-u1"


def _md(sid: str, body: str = "x") -> str:
    return f"---\nname: {sid}\ndescription: {sid} desc\n---\n{body}\n"


def _zip(sid: str, files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, data in files.items():
            zf.writestr(f"{sid}/{rel}", data)
    return buf.getvalue()


def _token(uid: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"u": uid}).encode()).decode().rstrip("=")
    return f"dcap2.{body}.sig"


STATE = {"cloud_base": "https://cloud.example", "token": _token("u-1"), "expires_at": 0}
PROFILE = profile_id(STATE["cloud_base"], "u-1")


class _Resp:
    def __init__(self, code, *, content=b"", body=None):
        self.status_code, self.content, self._body, self.headers = code, content, body, {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Cloud:
    def __init__(self, files_by_id: dict, suppressed=()):
        entries, self.bundles = [], {}
        for sid, files in files_by_id.items():
            md = files.get("SKILL.md") or _md(sid)
            extra = {k: v for k, v in files.items() if k != "SKILL.md"}
            h = skill_content_hash(md, extra)
            entries.append(
                {
                    "skill_id": sid,
                    "display_name": sid,
                    "description": "",
                    "version": "1",
                    "scope": "shared",
                    "content_hash": h,
                    "mcp_server_ids": [],
                }
            )
            self.bundles[sid] = _zip(sid, {"SKILL.md": md, **extra})
        self.manifest = build_skill_manifest(entries, list(suppressed))

    def get(self, url, headers=None, timeout=None):
        if url.endswith("/skills/manifest"):
            return _Resp(200, body={"data": self.manifest})
        for kind, endpoint in (("agent", "agents"), ("plugin", "plugins")):
            if url.endswith(f"/{endpoint}/manifest"):
                return _Resp(200, body={"data": build_entity_manifest(kind, [])})
        if "/skills/" in url:
            sid = url.rsplit("/skills/", 1)[1].split("/")[0]
            return _Resp(200, content=self.bundles[sid]) if sid in self.bundles else _Resp(404)
        return _Resp(404)


@pytest.fixture
def client(tmp_path, monkeypatch, index_db):
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "ws" / "skills"))
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(tmp_path / "caps"))
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "s")
    builtin = tmp_path / "builtin"
    (builtin / "ppt-design").mkdir(parents=True)
    (builtin / "ppt-design" / "SKILL.md").write_text(_md("ppt-design", "old"))
    monkeypatch.setattr(skill_config, "_builtin_skills_dir", lambda: builtin)
    monkeypatch.setattr(skills, "builtin_dir", lambda: builtin)
    bridge.reset_for_tests()
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: True)
    monkeypatch.setattr(skills, "current_local_user_id", lambda: USER)
    monkeypatch.setattr(bridge, "get_state", lambda: dict(STATE, expires_at=time.time() + 60))
    monkeypatch.setattr("core.agent_skills.cache_refresh.refresh_skill_caches", lambda: None)
    cloud = _Cloud(
        {"ppt-design": {"SKILL.md": _md("ppt-design", "cloud")}, "market-x": {"a.py": "1"}}
    )
    monkeypatch.setattr("httpx.get", cloud.get)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=USER, user_center_id="c1", username="t"
    )
    yield TestClient(app)
    bridge.reset_for_tests()


def _iid(sid: str) -> str:
    return registry.install_id(KIND_SKILL, PROFILE, sid)


def test_sync_prepares_everything(client):
    """登录后的这一次同步就把云端技能全部准备好——没有「待下载」这个中间态。"""
    r = client.post("/v1/desktop/capabilities/sync")
    assert r.status_code == 200
    status = r.json()["data"]
    assert status["pending_count"] == 0 and status["installed_count"] == 2

    r = client.get("/v1/desktop/capabilities/installations")
    items = {i["install_id"]: i for i in r.json()["data"]["items"]}
    assert items[_iid("market-x")]["state"] == "ready"
    assert items[_iid("ppt-design")]["resolution"] == {
        "outcome": "chosen",
        "reason": "account_unique",
    }
    assert items["skill:builtin:ppt-design"]["resolution"]["outcome"] == "shadowed"

    view = skill_config.get_user_skills_dir(USER)
    assert junction.is_directory_link(view / "market-x")
    assert "cloud" in (view / "ppt-design" / "SKILL.md").read_text()


def test_sync_is_idempotent(client):
    """重复同步不重复下载，也不改变结果。"""
    first = client.post("/v1/desktop/capabilities/sync").json()["data"]
    revisions = store.revisions(KIND_SKILL, PROFILE, "ppt-design")
    second = client.post("/v1/desktop/capabilities/sync").json()["data"]
    assert second["installed_count"] == first["installed_count"] == 2
    assert store.revisions(KIND_SKILL, PROFILE, "ppt-design") == revisions


def test_rebuild_views(client):
    client.post("/v1/desktop/capabilities/sync")
    r = client.post("/v1/desktop/capabilities/views/rebuild")
    assert r.status_code == 200 and "user" in r.json()["data"]


def test_manual_management_endpoints_are_gone(client):
    """手动准备 / 移除 / 同名选择 / 本机副本这些入口已经整体下线。"""
    assert client.post(
        "/v1/desktop/capabilities/preparations", json={"install_ids": [_iid("market-x")]}
    ).status_code == 404
    assert client.post(
        "/v1/desktop/capabilities/removals",
        json={"install_id": _iid("ppt-design"), "target": "device"},
    ).status_code == 404
    assert client.put(
        "/v1/desktop/capabilities/name-preferences",
        json={"runtime_name": "ppt-design", "install_id": "skill:builtin:ppt-design"},
    ).status_code == 404
    assert client.post(
        f"/v1/desktop/capabilities/installations/{_iid('ppt-design')}/local-copy", json={}
    ).status_code == 404


def test_endpoints_refuse_outside_desktop_store(client, monkeypatch):
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT")
    assert client.get("/v1/desktop/capabilities/installations").status_code == 403
