"""Hugo's skills and plugins are synced as files into /workspace/skills and /workspace/plugins on events."""
import json
import os

import pytest

from tests.test_personal_agent import sessions, personal_agent_on  # noqa: F401
from personal_agent.context import AgentContext
from personal_agent.capability_sync import background, plan, runner, store, tree
from personal_agent.skills import builtin_skills

BUILTINS = [skill["id"] for skill in builtin_skills()]


@pytest.fixture
def volume(monkeypatch, tmp_path):
    monkeypatch.setenv("PERSONAL_AGENT_VOLUME_ROOT", str(tmp_path / "volume"))
    monkeypatch.delenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", raising=False)
    return tmp_path / "volume"


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    """Two catalog skills and one plugin, with their directories on disk like the skill loader's."""
    root = tmp_path / "catalog"
    def skill(ident, body):
        (root / ident / "scripts").mkdir(parents=True, exist_ok=True)
        (root / ident / "SKILL.md").write_text("---\nname: " + ident + "\ndescription: d\n---\n" + body)
        (root / ident / "scripts" / "run.sh").write_text("echo hi\n")
        os.chmod(root / ident / "scripts" / "run.sh", 0o755)
    for ident in ("custom-skill", "other-skill", "plugin-skill"):
        skill(ident, ident + " v1")
    state = {"skills": ["custom-skill", "other-skill", "plugin-skill"], "mcps": ["plugin-tool"],
             "plugins": [{"install_id": "sample@owner", "slug": "sample", "name": "Sample", "enabled": True,
                          "skills": ["plugin-skill"], "mcp": ["plugin-tool"]}], "root": root, "skill": skill}

    class Loader:
        def get_skill_dir(self, ident):
            path = root / ident
            return str(path) if path.is_dir() else None
    monkeypatch.setattr("core.capabilities.skills.loader_factory.get_skill_loader", lambda reset=False: Loader())
    monkeypatch.setattr("core.capabilities.catalog.resolver.resolve_all_runtime_enabled",
                        lambda db, uid: (state["skills"], [], state["mcps"]))
    monkeypatch.setattr("core.capabilities.plugins.management.list_installed", lambda db, **kw: state["plugins"])
    monkeypatch.setattr("features.desktop.capabilities.device_catalog.plugin_entries", lambda user_id=None: [])
    return state


def workspace(volume, db):
    from personal_agent.models import PersonalAgent
    agent = db.query(PersonalAgent).filter(PersonalAgent.user_id == "owner").one()
    return volume / agent.id / "workspace"


def manifest(ws, kind):
    return json.loads((ws / kind / ".synced.json").read_text())


def test_binding_save_syncs_builtin_selected_and_plugin(sessions, volume, catalog):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        view = svc.identity.configure({"skill_ids": ["custom-skill"], "mcp_ids": [], "plugin_ids": ["sample@owner"]})
        ws = workspace(volume, db)
    status = view["config"]["capability_sync"]
    assert status["status"] == "ok" and status["error"] is None and status["skipped"] == []
    assert isinstance(status["synced_at"], float)
    skills = manifest(ws, "skills")
    assert skills["version"] == 1
    assert set(skills["skills"]) == {"custom-skill", *BUILTINS}
    assert skills["skills"]["custom-skill"]["source"] == "selected"
    assert all(skills["skills"][ident]["source"] == "builtin" for ident in BUILTINS)
    assert skills["skills"]["custom-skill"]["hash"] == tree.digest(tree.read_dir(catalog["root"] / "custom-skill"))
    assert (ws / "skills" / "custom-skill" / "SKILL.md").read_text().endswith("custom-skill v1")
    assert os.stat(ws / "skills" / "custom-skill" / "scripts" / "run.sh").st_mode & 0o111
    for ident in BUILTINS:
        assert (ws / "skills" / ident / "SKILL.md").is_file()
    # Plugin skills live only under the plugin.
    assert not (ws / "skills" / "plugin-skill").exists()
    plugins = manifest(ws, "plugins")
    entry = plugins["plugins"]["sample"]
    assert entry["install_id"] == "sample@owner" and entry["name"] == "Sample"
    assert entry["skills"] == {"plugin-skill": "skills/plugin-skill"} and entry["mcp_ids"] == ["plugin-tool"]
    assert entry["hash"] == tree.digest(tree.read_dir(ws / "plugins" / "sample"))
    assert json.loads((ws / "plugins" / "sample" / "plugin.json").read_text())["name"] == "sample"
    assert (ws / "plugins" / "sample" / "skills" / "plugin-skill" / "SKILL.md").is_file()
    # No staged leftovers.
    assert not [p for p in (ws / "skills").iterdir() if p.name.endswith(".tmp")]


def test_unbinding_removes_only_platform_copies(sessions, volume, catalog):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.configure({"skill_ids": ["custom-skill"], "mcp_ids": [], "plugin_ids": ["sample@owner"]})
        ws = workspace(volume, db)
        (ws / "skills" / "ctrip-hotel-price").mkdir()
        (ws / "skills" / "ctrip-hotel-price" / "SKILL.md").write_text("mine")
        view = svc.identity.configure({"skill_ids": ["other-skill"], "plugin_ids": []})
    assert view["config"]["capability_sync"]["status"] == "ok"
    assert not (ws / "skills" / "custom-skill").exists()
    assert (ws / "skills" / "other-skill" / "SKILL.md").is_file()
    assert (ws / "skills" / "ctrip-hotel-price" / "SKILL.md").read_text() == "mine"
    assert set(manifest(ws, "skills")["skills"]) == {"other-skill", *BUILTINS}
    assert not (ws / "plugins" / "sample").exists() and manifest(ws, "plugins")["plugins"] == {}


def test_hugo_own_dir_with_platform_id_wins_and_is_reported(sessions, volume, catalog):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.ensure()
        ws = workspace(volume, db)
        (ws / "skills" / "custom-skill").mkdir(parents=True)
        (ws / "skills" / "custom-skill" / "SKILL.md").write_text("Hugo wrote this")
        view = svc.identity.configure({"skill_ids": ["custom-skill"]})
    status = view["config"]["capability_sync"]
    assert status["skipped"] == [{"id": "custom-skill", "reason": "name_taken"}]
    assert (ws / "skills" / "custom-skill" / "SKILL.md").read_text() == "Hugo wrote this"
    assert "custom-skill" not in manifest(ws, "skills")["skills"]


def test_identical_unrecorded_copy_is_adopted(sessions, volume, catalog):
    """A sync that stopped after renaming a copy into place but before writing the manifest."""
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.ensure()
        ws = workspace(volume, db)
        import shutil
        shutil.copytree(catalog["root"] / "custom-skill", ws / "skills" / "custom-skill")
        view = svc.identity.configure({"skill_ids": ["custom-skill"]})
    assert view["config"]["capability_sync"]["skipped"] == []
    assert manifest(ws, "skills")["skills"]["custom-skill"]["source"] == "selected"


def test_upgrade_replaces_changed_and_keeps_unchanged(sessions, volume, catalog):
    with sessions() as db:
        AgentContext(db, "owner").identity.configure({"skill_ids": ["custom-skill", "other-skill"]})
        ws = workspace(volume, db)
        before = os.stat(ws / "skills" / "other-skill").st_ino
        catalog["skill"]("custom-skill", "custom-skill v2")
        assert runner.sync(db, "owner")["status"] == "ok"
    assert (ws / "skills" / "custom-skill" / "SKILL.md").read_text().endswith("v2")
    assert os.stat(ws / "skills" / "other-skill").st_ino == before


def test_revoked_skill_is_removed_on_event_sync(sessions, volume, catalog):
    with sessions() as db:
        AgentContext(db, "owner").identity.configure({"skill_ids": ["custom-skill"], "plugin_ids": ["sample@owner"]})
        ws = workspace(volume, db)
        catalog["skills"].remove("custom-skill")
        catalog["plugins"][0]["enabled"] = False
        runner.sync(db, "owner")
    assert not (ws / "skills" / "custom-skill").exists()
    assert not (ws / "plugins" / "sample").exists()


def test_catalog_outage_keeps_copies_and_reports_failure(sessions, volume, catalog):
    with sessions() as db:
        AgentContext(db, "owner").identity.configure({"skill_ids": ["custom-skill"]})
        ws = workspace(volume, db)
        catalog["skills"] = None
        status = runner.sync(db, "owner")
    assert status["status"] == "failed" and status["error"]
    assert (ws / "skills" / "custom-skill" / "SKILL.md").is_file()


def test_no_volume_reports_failure_without_breaking_save(sessions, catalog, monkeypatch):
    monkeypatch.delenv("PERSONAL_AGENT_VOLUME_ROOT", raising=False)
    monkeypatch.delenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", raising=False)
    with sessions() as db:
        view = AgentContext(db, "owner").identity.configure({"skill_ids": ["custom-skill"]})
    assert view["config"]["skill_ids"] == ["custom-skill"]
    assert view["config"]["capability_sync"]["status"] == "failed"


def test_links_in_workspace_are_never_followed(sessions, volume, catalog, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    with sessions() as db:
        AgentContext(db, "owner").identity.configure({"skill_ids": ["custom-skill"]})
        ws = workspace(volume, db)
        # The agent swaps a synced copy for a link out of the workspace.
        import shutil
        shutil.rmtree(ws / "skills" / "custom-skill")
        os.symlink(outside, ws / "skills" / "custom-skill")
        assert runner.sync(db, "owner")["status"] == "ok"
        assert (ws / "skills" / "custom-skill").is_dir() and not (ws / "skills" / "custom-skill").is_symlink()
        AgentContext(db, "owner").identity.configure({"skill_ids": []})
    assert (outside / "keep.txt").read_text() == "keep"
    # A /workspace/skills that is itself a link is refused.
    with sessions() as db:
        ws = workspace(volume, db)
    shutil.rmtree(ws / "skills")
    os.symlink(outside, ws / "skills")
    with sessions() as db:
        assert runner.sync(db, "owner")["status"] == "failed"
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]


def test_tampered_manifest_cannot_delete_outside(volume, tmp_path):
    ws = volume / "pa_x" / "workspace"
    (ws / "skills").mkdir(parents=True)
    (volume / "pa_x" / "precious").write_text("x")
    (ws / "skills" / ".synced.json").write_text(json.dumps(
        {"version": 1, "skills": {"../precious": {"source": "selected", "hash": "h"}, "..": {"source": "selected"}}}))
    status = runner.apply("pa_x", plan.Plan(skills={}, plugins={}))
    assert status["status"] == "ok"
    assert (volume / "pa_x" / "precious").exists()


def test_capability_options_list_builtin_skills(sessions, catalog):
    from personal_agent.api.identity import capability_options
    with sessions() as db:
        data = capability_options(svc=AgentContext(db, "owner"))["data"]
    assert [s["id"] for s in data["builtin_skills"]] == BUILTINS
    macos = next(s for s in data["builtin_skills"] if s["id"] == "pa-computer-macos")
    assert macos["name"] != "pa-computer-macos" and macos["description"]
    assert set(macos) == {"id", "name", "description"}


def test_background_queue_admin_event_and_builtin_check(sessions, volume, catalog, monkeypatch):
    queue = background.Queue(debounce=0)
    monkeypatch.setattr(background, "queue", queue)
    monkeypatch.setattr("personal_agent.capability_sync.queue", queue)
    monkeypatch.setattr(background, "_checked", set())
    with sessions() as db:
        AgentContext(db, "owner").identity.configure({"skill_ids": ["custom-skill"]})
        ws = workspace(volume, db)
        # New agents default to the enabled plugins; "other" starts with none.
        plugins, catalog["plugins"] = catalog["plugins"], []
        AgentContext(db, "other").identity.ensure()
        catalog["plugins"] = plugins
    from personal_agent import capability_sync
    assert capability_sync.request_all() is False  # not started: nothing runs in the background
    queue.start()
    # An admin upgrade: the core skill refresh stage reaches the registered listener.
    from core.harness.registry.cache_invalidation import cache_invalidators
    monkeypatch.setattr(cache_invalidators(), "callbacks", {})
    monkeypatch.setattr("core.harness.registry.registry.registration_state", lambda: type("S", (), {"lifecycle": []})())
    capability_sync.install()
    catalog["skill"]("custom-skill", "custom-skill v3")
    cache_invalidators().run("after_skill_refresh")
    assert queue.idle()
    assert (ws / "skills" / "custom-skill" / "SKILL.md").read_text().endswith("v3")
    # "other" never bound anything: the admin sweep leaves it alone; the built-in check syncs it once.
    with sessions() as db:
        from personal_agent.models import PersonalAgent
        other = db.query(PersonalAgent).filter(PersonalAgent.user_id == "other").one()
        assert "capability_sync" not in (other.config or {})
        assert background.check_builtins(other.id, "other") is True
        assert queue.idle()
        assert background.check_builtins(other.id, "other") is False  # once per process
        db.expire_all()
        other = db.get(PersonalAgent, other.id)
        assert other.config["capability_sync"]["status"] == "ok"
    synced = manifest(volume / other.id / "workspace", "skills")["skills"]
    assert {k: v["hash"] for k, v in synced.items()} == plan.builtin_hashes()
    # A deploy that changed a built-in: the next process sees a different hash and resyncs.
    monkeypatch.setattr(background, "_checked", set())
    monkeypatch.setattr(plan, "builtin_hashes", lambda: {"pa-new": "h"})
    assert background.check_builtins(other.id, "other") is True
    assert queue.idle()
