from personal_agent.identity import capabilities as identity_capabilities
"""Personal Agent tool, device and profile regressions, split to keep files under 500 lines."""
import personal_agent.workspace.files as _pa_files
import pytest
from personal_agent.context import AgentContext
from tests.test_personal_agent import personal_agent_on, sessions  # noqa: F401

@pytest.mark.asyncio
async def test_exports_with_same_requested_name_create_distinct_current_files(sessions, monkeypatch):
    import json, time
    from types import SimpleNamespace
    from personal_agent.tools import tools
    from personal_agent.models import PersonalWorkItem
    from core.db.models import Artifact
    from core.files.storage import get_storage
    from core.files.artifacts import service as artifact_service
    class Storage:
        def __init__(self): self.files = {}
        def upload_bytes(self, content, key): self.files[key] = content; return key
    storage = Storage()
    import core.files.storage
    monkeypatch.setattr(core.files.storage, "get_storage", lambda: storage)
    monkeypatch.setattr(tools, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    from personal_agent.tools import space
    from personal_agent.tools import email
    from personal_agent.tasks.execution import current_execution
    current_execution.set(None)
    monkeypatch.setattr(space, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr(email, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    class Provider:
        content = b"current-first"
        async def get_file(self, *args, **kwargs): return self.content
    provider = Provider()
    monkeypatch.setattr(tools, "get_personal_sandbox_provider", lambda: provider)
    class Toolkit:
        def __init__(self): self.functions = {}
        def append(self, tool):
            fn = tool._func
            self.functions[fn.__name__] = fn
    with sessions() as db:
        task = AgentContext(db,"owner").tasks.submit("Export file","one")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time()+90
        db.commit()
    kit = Toolkit()
    tools.register_personal_tools(kit,SimpleNamespace(profile_ref=task["id"],current_user_id="owner"))
    from personal_agent.delivery import delivery
    monkeypatch.setattr(delivery, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    first=json.loads((await kit.functions["pa_deliver"](request_key="first", path="/workspace/work/output.txt", filename="output.txt")).content[0].text)
    provider.content=b"current-second"
    second=json.loads((await kit.functions["pa_deliver"](request_key="second", path="/workspace/work/output.txt", filename="output.txt")).content[0].text)
    assert first["artifact_id"] != second["artifact_id"]
    with sessions() as db:
        a,b=db.get(Artifact,first["artifact_id"]),db.get(Artifact,second["artifact_id"])
        assert a.filename != b.filename
        assert storage.files[a.storage_key] == b"current-first"
        assert storage.files[b.storage_key] == b"current-second"


@pytest.mark.asyncio
async def test_desktop_lease_release_allows_next_action_without_releasing_another_owner():
    from core.infra.ephemeral import LocalEphemeralState
    state=LocalEphemeralState()
    assert await state.hold("desktop", "first", ttl=45)
    assert not await state.release("desktop", "wrong-owner")
    assert not await state.hold("desktop", "second", ttl=45)
    assert await state.release("desktop", "first")
    assert await state.hold("desktop", "second", ttl=45)
    assert not await state.release("desktop", "first")


@pytest.mark.asyncio
async def test_cancellation_during_approved_dispatch_stays_cancelled(sessions, monkeypatch):
    import time
    from contextlib import asynccontextmanager
    from personal_agent.tasks import worker as module
    from personal_agent.models import PersonalWorkItem
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    @asynccontextmanager
    async def binding(**kwargs): yield object()
    monkeypatch.setattr(module, "durable_run_binding", binding)
    worker = module.PersonalAgentWorker()
    with sessions() as db:
        task=AgentContext(db,"owner").tasks.submit("Send file","one")
        row=db.get(PersonalWorkItem,task["id"])
        row.status,row.owner,row.run_id,row.lease_until="running",worker.identity,"run",time.time()+90
        row.checkpoint={"task_wait_reason":"Device offline"};db.commit()
    async def dispatch(ident, uid):
        with sessions() as db:AgentContext(db,uid).tasks.cancel(ident)
    monkeypatch.setattr(worker,"dispatch_approved",dispatch)
    await worker.run(task["id"])
    with sessions() as db:assert db.get(PersonalWorkItem,task["id"]).status=="cancelled"


@pytest.mark.asyncio
async def test_edit_file_replaces_one_exact_entry_after_read(sessions, monkeypatch, tmp_path):
    import json, time
    from types import SimpleNamespace
    from personal_agent.tools import tools
    from personal_agent.models import PersonalWorkItem
    from personal_agent.tasks.execution import current_execution
    current_execution.set(None)
    from personal_agent.tools import space
    from personal_agent.tools import email
    for module in (tools, space, email):
        monkeypatch.setattr(module, "SessionLocal", sessions)
        monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    class Provider:
        files = {"/workspace/USER.md": "- 优先简洁\n- 总是中文\n- 总是中文\n".encode()}
        async def get_file(self, box, path, user_id=None): return self.files[path]
        async def put_file(self, box, path, content, user_id=None): self.files[path] = content.encode() if isinstance(content, str) else content
    provider = Provider()
    monkeypatch.setattr(tools, "get_personal_sandbox_provider", lambda: provider)
    class Toolkit:
        def __init__(self): self.functions = {}
        def append(self, tool):
            fn = tool._func
            self.functions[fn.__name__] = fn
    with sessions() as db:
        task = AgentContext(db, "owner").tasks.submit("Remember preference", "edit-one")
        row = db.get(PersonalWorkItem, task["id"])
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 90
        db.commit()
    kit = Toolkit()
    tools.register_personal_tools(kit, SimpleNamespace(profile_ref=task["id"], current_user_id="owner"))
    from personal_agent.workspace import persona
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    with sessions() as db:
        agent_id = db.get(PersonalWorkItem, task["id"]).agent_id
    root = _pa_files.local_workspace(agent_id)
    root.mkdir(parents=True)
    (root / "USER.md").write_bytes(provider.files["/workspace/USER.md"])
    edit = kit.functions["pa_edit_file"]
    with pytest.raises(ValueError, match="Read the file"):
        await edit("/workspace/USER.md", "优先简洁", "优先详细")
    await kit.functions["pa_read_file"]("/workspace/USER.md")
    with pytest.raises(ValueError, match="occurs 2 times"):
        await edit("/workspace/USER.md", "总是中文", "总是英文")
    with pytest.raises(ValueError, match="not found"):
        await edit("/workspace/USER.md", "不存在", "x")
    with pytest.raises(ValueError, match="inside /workspace"):
        await edit("/etc/passwd", "a", "b")
    result = json.loads((await edit("/workspace/USER.md", "优先简洁", "优先详细")).content[0].text)
    assert result["replacements"] == 1
    result = json.loads((await edit("/workspace/USER.md", "总是中文", "总是英文", True)).content[0].text)
    assert result["replacements"] == 2
    assert (root / "USER.md").read_text() == "- 优先详细\n- 总是英文\n- 总是英文\n"


def test_default_name_is_hugo_and_renames_reach_title_and_identity(sessions, tmp_path, monkeypatch):
    from core.db.models import ChatSession
    from personal_agent.workspace import persona
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    with sessions() as db:
        svc = AgentContext(db, "owner")
        view = svc.identity.ensure()
        assert view["config"]["name"] == "Hugo"
        assert db.get(ChatSession, view["chat_id"]).title == "Hugo"
        persona.seed(view["id"], view["config"]["name"])
        identity = tmp_path / view["id"] / "workspace" / "IDENTITY.md"
        assert "- **名字：** Hugo" in identity.read_text()
        # Settings or the agent's pa_set_name: config, conversation title and IDENTITY.md follow.
        svc.identity.configure({"name": "阿福"})
        assert db.get(ChatSession, view["chat_id"]).title == "阿福"
        assert "- **名字：** 阿福" in identity.read_text()
        # A hand-edited IDENTITY.md renames the agent too; a placeholder does not.
        svc.identity.adopt_identity_name("- **名字：** 小跟班\n")
        assert svc.identity.view()["config"]["name"] == "小跟班"
        assert db.get(ChatSession, view["chat_id"]).title == "小跟班"
        svc.identity.adopt_identity_name("- **名字：** _(待定)_\n")
        assert svc.identity.view()["config"]["name"] == "小跟班"


@pytest.mark.parametrize("written, expected", [("Personal Agent", "Hugo"), ("旺财", "旺财")])
def test_unnamed_agent_upgrade_keeps_a_self_chosen_name(sessions, tmp_path, monkeypatch, written, expected):
    from core.db.models import ChatSession
    from personal_agent.workspace import persona
    from personal_agent.models import PersonalAgent
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    with sessions() as db:
        svc = AgentContext(db, "owner")
        view = svc.identity.ensure()
        persona.seed(view["id"], written)
        row = db.get(PersonalAgent, view["id"])
        row.config = {key: value for key, value in row.config.items() if key != "name"} | {"defaults_version": 2, "mcp_ids": []}
        db.commit()
        config = svc.identity.ensure()["config"]
        # Capabilities are derived, not chosen: an emptied tool list comes back.
        assert config["name"] == expected and config["mcp_ids"] == ["internet_search", "web_fetch"]
        assert config["space_enabled"] and config["email_enabled"]
        assert db.get(ChatSession, view["chat_id"]).title == expected
        assert "- **名字：** " + expected in (tmp_path / view["id"] / "workspace" / "IDENTITY.md").read_text()


def test_capabilities_follow_what_the_user_may_use(sessions, monkeypatch):
    from personal_agent import context as module
    from personal_agent.identity import config as identity_config
    permitted = {"skills": ["officecli-docx", "translate", "some-other-skill"], "mcps": ["internet_search", "web_fetch", "database_query"]}
    monkeypatch.setattr('core.capabilities.catalog.resolver.resolve_all_runtime_enabled',
                        lambda db, uid: (permitted["skills"], [], permitted["mcps"]))
    with sessions() as db:
        svc = module.AgentContext(db, "owner")
        config = svc.identity.ensure()["config"]
        assert config["skill_ids"] == ["officecli-docx", "translate"]
        assert config["mcp_ids"] == ["internet_search", "web_fetch"]
        permitted["skills"] = ["officecli-docx"]
        assert svc.identity.ensure()["config"]["skill_ids"] == ["officecli-docx"]
        # An unreadable catalog keeps the last known set instead of emptying it.
        permitted["skills"] = permitted["mcps"] = None
        assert svc.identity.ensure()["config"]["skill_ids"] == ["officecli-docx"]
        with pytest.raises(ValueError): svc.identity.configure({"skill_ids": ["translate"]})


def test_enabled_plugins_join_with_their_components(sessions, monkeypatch):
    from personal_agent import context as module
    from personal_agent.identity import config as identity_config
    monkeypatch.setattr('core.capabilities.catalog.resolver.resolve_all_runtime_enabled',
                        lambda db, uid: (["officecli-docx", "feishu-doc"], [], ["internet_search", "feishu_mcp"]))
    installed = [
        {"install_id": "feishu@owner", "slug": "feishu", "name": "Feishu", "enabled": True, "callable": True,
         "skills": ["feishu-doc", "feishu-off"], "mcp": ["feishu_mcp"]},
        {"install_id": "off@owner", "slug": "off", "name": "Off", "enabled": False, "callable": True, "skills": ["x"], "mcp": []},
    ]
    monkeypatch.setattr('core.capabilities.plugins.management.list_installed', lambda db, **kw: installed)
    monkeypatch.setattr('features.desktop.capabilities.device_catalog.plugin_entries', lambda user_id=None: [])
    with sessions() as db:
        svc = module.AgentContext(db, "owner")
        config = svc.identity.ensure()["config"]
        # Only the plugin the user keeps on, with only its enabled components.
        assert config["plugins"] == [{"id": "feishu@owner", "name": "Feishu", "skill_ids": ["feishu-doc"], "mcp_ids": ["feishu_mcp"]}]
        assert identity_capabilities.capability_ids(config) == (["officecli-docx", "feishu-doc"], ["internet_search", "feishu_mcp"])
        # An unreadable plugin listing keeps the last known set instead of breaking the agent.
        monkeypatch.setattr('core.capabilities.plugins.management.list_installed', lambda db, **kw: 1 / 0)
        assert svc.identity.ensure()["config"]["plugins"][0]["id"] == "feishu@owner"
        monkeypatch.setattr('core.capabilities.plugins.management.list_installed', lambda db, **kw: installed)
        # The conversation turn only answers and delegates: plugin tools stay with the tasks.
        assert identity_capabilities.front_mcp_ids(config) == ["internet_search"]
        installed[0]["enabled"] = False
        assert svc.identity.ensure()["config"]["plugins"] == []


def test_task_prompt_lists_skills_with_when_to_use_them():
    from types import SimpleNamespace
    from personal_agent import context as module
    from personal_agent.identity import config as identity_config

    class Loader:
        def load_all_metadata(self):
            return {"translate": SimpleNamespace(description="Translate text\n between languages. " + "x" * 300)}

    lines = identity_config.skill_summaries(["translate", "unknown"], loader=Loader())
    assert lines[0].startswith("- `translate`: Translate text between languages.") and lines[0].endswith("…")
    assert len(lines[0]) <= identity_config.SKILL_SUMMARY_LENGTH + 20
    # A skill without metadata still shows its id, so pa_skill_read can find it.
    assert lines[1] == "- `unknown`"
    assert identity_config.skill_summaries([]) == []



def test_model_choice_uses_the_chat_allowlist(sessions, monkeypatch):
    calls = []
    def resolve(db, pid, user_id=None):
        calls.append(pid)
        return pid if pid == "allowed" else None
    monkeypatch.setattr('core.auth.model_selection.resolve_user_model_provider_id', resolve)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        assert svc.identity.configure({"model_provider_id": "allowed"})["config"]["model_provider_id"] == "allowed"
        with pytest.raises(ValueError): svc.identity.configure({"model_provider_id": "blocked"})
        assert svc.identity.configure({"model_provider_id": ""})["config"]["model_provider_id"] is None
        assert calls == ["allowed", "blocked"]
