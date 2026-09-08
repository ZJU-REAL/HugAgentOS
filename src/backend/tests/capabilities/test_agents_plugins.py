"""智能体 / 插件：协议、云端清单与定义包、本机落盘、组件归属与就绪、名称裁决与列表合并。"""

from __future__ import annotations

import base64
import io
import json
import time
import zipfile
from datetime import datetime

import pytest
from core.capabilities import agents as caps_agents
from core.capabilities import plugins as caps_plugins
from core.capabilities import registry, store
from core.capabilities.paths import KIND_AGENT, KIND_PLUGIN, KIND_SKILL
from core.capabilities.ref import profile_id
from core.db.engine import Base
from core.db.models import InstalledPlugin, UserAgent, UserShadow
from core.services import desktop_capability as cloud
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_bundles as bundles
from core.services.desktop_capability_protocol import (
    CapabilityManifestError,
    build_entity_manifest,
    entity_content_hash,
    validate_entity_manifest,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _token(uid: str) -> str:
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"u": uid, "c": "center-" + uid, "a": 1, "h": "session-" + uid, "d": "device"}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"dcap2.{body}.sig"


STATE = {"cloud_base": "https://cloud.example", "token": _token("cloud-u"), "expires_at": 0}
PROFILE = profile_id(STATE["cloud_base"], "cloud-u")


def test_entity_manifest_roundtrip_and_strictness():
    entry = {
        "agent_id": "ua_1",
        "name": "写手",
        "description": "",
        "version": "V1.0",
        "content_hash": "a" * 64,
        "is_enabled": True,
    }
    m = build_entity_manifest("agent", [entry])
    assert validate_entity_manifest(json.loads(json.dumps(m)), "agent") == m
    with pytest.raises(CapabilityManifestError):
        validate_entity_manifest({**m, "kind": "plugin"}, "plugin")
    bad = json.loads(json.dumps(m))
    bad["entries"][0]["name"] = "改了"
    with pytest.raises(CapabilityManifestError):
        validate_entity_manifest(bad, "agent")


# ── 云端侧：从真实表构建清单与定义包 ──────────────────────────────────


@pytest.fixture
def cloud_db(tmp_path, monkeypatch):
    import core.db.models  # noqa: F401

    engine = create_engine(
        f"sqlite:///{tmp_path / 'cloud.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cloud, "SessionLocal", factory)
    monkeypatch.setattr(
        "core.services.plugin_service._has_admin_config_for_slug", lambda slug: False
    )
    with factory() as db:
        db.add(UserShadow(user_id="cloud-u", username="c"))
        db.add(
            UserAgent(
                agent_id="ua_writer",
                owner_type="user",
                user_id="cloud-u",
                name="写手",
                system_prompt="你是写手",
                skill_ids=["word-editing"],
                mcp_server_ids=["internet_search"],
                is_enabled=True,
                extra_config={"version": "V1.2", "change_history": [{"v": 1}]},
            )
        )
        db.add(
            InstalledPlugin(
                install_id="sites@cloud-u",
                slug="sites",
                name="站点",
                version="1.0.0",
                owner_user_id="cloud-u",
                source="builtin",
                component_ids={
                    "skills": ["sites-site-builder"],
                    "mcp": ["sites-site_publish"],
                    "prompts": [],
                },
                created_at=datetime.utcnow(),
            )
        )
        db.commit()
    cloud._entity_manifest_cache.clear()
    return factory


def test_cloud_manifests_and_bundles(cloud_db):
    am = cloud.build_user_agent_manifest("cloud-u", use_cache=False)
    assert [e["agent_id"] for e in am["entries"]] == ["ua_writer"]
    data, h = cloud.resolve_agent_bundle("cloud-u", "ua_writer")
    assert h == am["entries"][0]["content_hash"]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = sorted(zf.namelist())
        assert names == ["ua_writer/agent.json", "ua_writer/instructions.md"]
        agent_json = json.loads(zf.read("ua_writer/agent.json"))
        assert (
            agent_json["skill_ids"] == ["word-editing"]
            and "change_history" not in agent_json["extra_config"]
        )
        assert zf.read("ua_writer/instructions.md").decode() == "你是写手"
    assert cloud.resolve_agent_bundle("other-user", "ua_writer") is None

    pm = cloud.build_user_plugin_manifest("cloud-u", use_cache=False)
    assert pm["entries"][0]["install_id"] == "sites@cloud-u" and pm["entries"][0]["skills"] == [
        "sites-site-builder"
    ]
    data, h = cloud.resolve_plugin_bundle("cloud-u", "sites@cloud-u")
    assert h == pm["entries"][0]["content_hash"]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        plugin_json = json.loads(zf.read("sites/plugin.json"))
        assert plugin_json["components"]["mcp"] == ["sites-site_publish"]


# ── 本机侧：同步 + 落盘 + 组件归属 ─────────────────────────────────────


class _Resp:
    def __init__(self, code, *, content=b"", body=None):
        self.status_code, self.content, self._body, self.headers = code, content, body, {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _zip(root: str, files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, body in files.items():
            zf.writestr(f"{root}/{rel}", body)
    return buf.getvalue()


class _Cloud:
    def __init__(self, agents: dict, plugins: dict):
        self.bundles = {}
        a_entries, p_entries = [], []
        for aid, (definition, prompt) in agents.items():
            files = {
                "agent.json": json.dumps(definition, sort_keys=True),
                "instructions.md": prompt,
            }
            a_entries.append(
                {
                    "agent_id": aid,
                    "name": definition["name"],
                    "description": "",
                    "version": "V1.0",
                    "content_hash": entity_content_hash(files),
                    "is_enabled": True,
                }
            )
            self.bundles[("agents", aid)] = _zip(aid, files)
        for iid, definition in plugins.items():
            files = {"plugin.json": json.dumps(definition, sort_keys=True)}
            p_entries.append(
                {
                    "install_id": iid,
                    "slug": definition["slug"],
                    "name": definition["name"],
                    "version": "1",
                    "description": "",
                    "category": "",
                    "content_hash": entity_content_hash(files),
                    "enabled": True,
                    "skills": definition["components"]["skills"],
                    "mcp": definition["components"]["mcp"],
                }
            )
            self.bundles[("plugins", iid)] = _zip(definition["slug"], files)
        self.manifests = {
            "agents": build_entity_manifest("agent", a_entries),
            "plugins": build_entity_manifest("plugin", p_entries),
        }

    def get(self, url, headers=None, timeout=None):
        if url.endswith("/skills/manifest"):
            from core.services.desktop_capability_protocol import build_skill_manifest

            return _Resp(200, body={"data": build_skill_manifest([], [])})
        for kind in ("agents", "plugins"):
            if url.endswith(f"/{kind}/manifest"):
                return _Resp(200, body={"data": self.manifests[kind]})
            marker = f"/{kind}/"
            if marker in url and url.endswith("/bundle"):
                ident = url.split(marker, 1)[1][: -len("/bundle")]
                key = (kind, ident)
                return _Resp(200, content=self.bundles[key]) if key in self.bundles else _Resp(404)
        return _Resp(404)


@pytest.fixture
def device(tmp_path, monkeypatch, index_db, caps_root):
    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "ws" / "skills"))
    monkeypatch.setenv("HUGAGENT_DESKTOP_BRIDGE_SECRET", "s")
    bridge.reset_for_tests()
    bundles.reset_for_tests()
    monkeypatch.setattr(bridge, "bridge_enabled", lambda: True)
    monkeypatch.setattr(bridge, "get_state", lambda: dict(STATE, expires_at=time.time() + 60))
    monkeypatch.setattr("core.capabilities.skills.current_local_user_id", lambda: "u")
    yield
    bridge.reset_for_tests()
    bundles.reset_for_tests()


def _agent_def(name: str, **extra) -> dict:
    return {
        "agent_id": extra.get("agent_id", "ua_cloud"),
        "owner_type": "user",
        "name": name,
        "description": "",
        "skill_ids": ["word-editing"],
        "mcp_server_ids": [],
        "plugin_ids": [],
        "kb_ids": [],
        "is_enabled": True,
        "max_iters": 8,
        "timeout": 60,
        "extra_config": {},
        "version": "V1.0",
    }


def test_sync_prepares_definitions_and_plugin_readiness(device, monkeypatch):
    fake = _Cloud(
        agents={"ua_cloud": (_agent_def("写手"), "你是写手")},
        plugins={
            "sites@u": {
                "install_id": "sites@u",
                "slug": "sites",
                "name": "站点",
                "components": {"skills": ["sites-site-builder"], "mcp": ["sites-site_publish"]},
            }
        },
    )
    monkeypatch.setattr("httpx.get", fake.get)
    bundles.sync_blocking(STATE)

    inst = registry.get(registry.install_id(KIND_AGENT, PROFILE, "ua_cloud"))
    assert inst.ready
    defn = caps_agents.account_definition("ua_cloud")
    assert (
        defn.name == "写手"
        and defn.system_prompt == "你是写手"
        and defn.max_iters == 8
        and defn.origin == "cloud"
    )
    assert caps_agents.account_definition("nope") is None

    pinst = registry.get(registry.install_id(KIND_PLUGIN, PROFILE, "sites"))
    assert pinst.ready and pinst.payload["cloud_install_id"] == "sites@u"
    edges = registry.components_of(pinst.install_id)
    assert set(edges) == {
        f"skill:{PROFILE}:sites-site-builder",
        f"mcp:{PROFILE}:sites-site_publish",
    }
    ready = caps_plugins.readiness(pinst, cloud_server_ids=["sites-site_publish"])
    assert ready["ready"] is False and ready["missing_required"] == [
        f"skill:{PROFILE}:sites-site-builder"
    ]
    registry.upsert(
        profile_id=PROFILE,
        ref=__import__("core.capabilities.ref", fromlist=["cloud_ref"]).cloud_ref(
            STATE["cloud_base"], KIND_SKILL, "sites-site-builder", scope="shared"
        ),
        content_hash="b" * 64,
    )
    store.write_from_files(
        KIND_SKILL, PROFILE, "sites-site-builder", "bbbbbbbbbbbb", {"SKILL.md": "site builder"}
    )
    registry.set_state(
        f"skill:{PROFILE}:sites-site-builder", "ready", resolved_revision="bbbbbbbbbbbb"
    )
    assert caps_plugins.readiness(pinst, cloud_server_ids=["sites-site_publish"])["ready"] is True
    assert caps_plugins.readiness(pinst, cloud_server_ids=[])["ready"] is False

    # 清单里消失 → 撤销授权；保留历史引用所需的不可变内容
    empty = _Cloud(agents={}, plugins={})
    monkeypatch.setattr("httpx.get", empty.get)
    bundles.reset_for_tests()
    bundles.sync_blocking(STATE)
    assert registry.list_installations(kind=KIND_AGENT, profile_id=PROFILE) == []
    assert len(store.revisions(KIND_AGENT, PROFILE, "ua_cloud")) == 1
    assert caps_agents.account_definition("ua_cloud") is None


def test_tampered_definition_is_rejected(device, monkeypatch):
    fake = _Cloud(agents={"ua_cloud": (_agent_def("写手"), "真")}, plugins={})
    fake.bundles[("agents", "ua_cloud")] = _zip(
        "ua_cloud", {"agent.json": "{}", "instructions.md": "假"}
    )
    monkeypatch.setattr("httpx.get", fake.get)
    bundles.sync_blocking(STATE)
    inst = registry.get(registry.install_id(KIND_AGENT, PROFILE, "ua_cloud"))
    assert inst.state == "failed" and "hash" in (inst.last_error or "")
    assert caps_agents.account_definitions() == []


def test_visible_list_merges_and_resolves_names(device, monkeypatch):
    fake = _Cloud(agents={"ua_cloud": (_agent_def("写手"), "云端")}, plugins={})
    monkeypatch.setattr("httpx.get", fake.get)
    bundles.sync_blocking(STATE)
    local_rows = [
        {**_agent_def("本地助手", agent_id="ua_local"), "system_prompt": "本地", "user_id": "u"},
        {**_agent_def("写手", agent_id="ua_dup"), "system_prompt": "另一份", "user_id": "u"},
    ]
    visible = caps_agents.merge_visible("u", local_rows)
    names = sorted(v["name"] for v in visible)
    assert names == ["本地助手"]  # 同名冲突：两份都不可见，直到用户选择
    res = caps_agents.last_resolution("u")
    assert "写手" in res.conflicts
    registry.set_preference(
        KIND_AGENT, "写手", registry.install_id(KIND_AGENT, PROFILE, "ua_cloud"), chosen_by="u"
    )
    visible = caps_agents.merge_visible("u", local_rows)
    chosen = {v["agent_id"]: v for v in visible}
    assert set(chosen) == {"ua_local", "ua_cloud"} and chosen["ua_cloud"]["origin"] == "cloud"


def test_local_agent_projection(device):
    row = {**_agent_def("本地", agent_id="ua_l"), "system_prompt": "p", "user_id": "u"}
    comp = caps_agents.publish_local_agent(row)
    assert (comp.path / "instructions.md").read_text() == "p"
    row["system_prompt"] = "p2"
    comp2 = caps_agents.publish_local_agent(row)
    assert comp2.revision != comp.revision
    assert {c.revision for c in store.revisions(KIND_AGENT, "local", "ua_l")} == {
        comp.revision,
        comp2.revision,
    }
    assert (comp.path / "instructions.md").read_text() == "p"
    assert (comp2.path / "instructions.md").read_text() == "p2"
    assert (
        caps_agents.remove_local_agent("ua_l")
        and store.revisions(KIND_AGENT, "local", "ua_l") == []
    )


def test_local_plugin_projection(device):
    definition = {
        "install_id": "sites@u",
        "slug": "sites",
        "name": "站点",
        "version": "1",
        "components": {"skills": ["sites-a"], "mcp": ["sites-site_publish"]},
    }
    comp = caps_plugins.publish_local_plugin(definition, owner_user_id="u")
    assert json.loads((comp.path / "plugin.json").read_text())["slug"] == "sites"
    assert set(registry.components_of("plugin:local:sites")) == {
        "skill:local:sites-a",
        "mcp:local:sites-site_publish",
    }
    assert (
        caps_plugins.remove_local_plugin("sites")
        and registry.components_of("plugin:local:sites") == {}
    )
