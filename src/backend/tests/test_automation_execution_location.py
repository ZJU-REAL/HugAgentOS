import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.db.engine import Base
from core.db.models import Project
from core.services.automation_service import AutomationService


@pytest.fixture
def local_project(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    monkeypatch.setenv("SCRIPT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    (tmp_path / "AGENTS.md").write_text("Summarize changes", encoding="utf-8")
    engine = create_engine(f"sqlite:///{tmp_path / 'local.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    import core.db.engine as engine_module
    monkeypatch.setattr(engine_module, "SessionLocal", factory)
    with factory() as db:
        db.add(Project(project_id="local-project", owner_user_id="owner", kind="local",
                       name="Reports", instructions="Summarize changes",
                       extra_data={"local": {"path": str(tmp_path), "slug": "reports"}}))
        db.commit()
    yield factory, tmp_path
    engine.dispose()



@pytest.fixture
def desktop_identity(local_project, monkeypatch):
    import base64
    import json
    import time
    from core.capabilities import registry, skills
    from core.db.models import UserShadow
    from core.services import desktop_cloud_bridge as bridge

    factory, _ = local_project
    monkeypatch.setattr(registry, "SessionLocal", factory)
    monkeypatch.setattr(skills, "_shadow_user_ids", {})
    monkeypatch.setattr(bridge, "_state_loaded", True)
    monkeypatch.setattr(bridge, "_state", None)
    with factory() as db:
        db.add(UserShadow(user_id="owner", username="Alice",
                          user_center_id="cloud:example.com:443:account-a"))
        db.add(UserShadow(user_id="other", username="Bob",
                          user_center_id="cloud:example.com:443:account-b"))
        db.commit()

    def switch(account="account-a", subject="cloud-owner", cloud_base="https://example.com"):
        claims = base64.urlsafe_b64encode(json.dumps(
            {"u": subject, "c": account, "d": "test-device", "a": 1}
        ).encode()).decode().rstrip("=")
        bridge._state = {"cloud_base": cloud_base, "token": f"dcap2.{claims}.fixture",
                         "expires_at": time.time() + 600}
    switch()
    return switch

def test_local_task_captures_project_and_revalidates_binding(local_project, monkeypatch):
    from core.services.automation_execution import task_execution_context
    factory, path = local_project
    with factory() as db:
        service = AutomationService(db)
        task = service.create_task(user_id="owner", task_type="prompt", prompt="report",
                                   cron_expression="0 18 * * *", execution_location="local",
                                   project_id="local-project")
        brief = service.task_to_dict(task)
        assert brief["execution_location"] == "local"
        assert brief["project_id"] == "local-project"
        assert brief["device_id"] and brief["device_name"]
        context = task_execution_context(db, task)
        assert context["project_id"] == "local-project"
        assert context["project_local_path"] == str(path)
        assert context["project_is_local"] is True
        with pytest.raises(ValueError):
            service.create_task(user_id="other", task_type="prompt", prompt="report",
                                cron_expression="0 18 * * *", execution_location="local",
                                project_id="local-project")
        with pytest.raises(ValueError):
            service.create_task(user_id="owner", task_type="prompt", prompt="report",
                                cron_expression="0 18 * * *", execution_location="cloud",
                                project_id="local-project")
        project = db.get(Project, "local-project")
        project.extra_data = {"local": {"path": str(path / "moved"), "slug": "reports"}}
        db.commit()
        with pytest.raises(ValueError, match="directory|目录"):
            task_execution_context(db, task)


@pytest.mark.asyncio
async def test_scheduler_restores_project_into_real_run_context(local_project, monkeypatch):
    factory, path = local_project
    from orchestration.schedulers.automation_scheduler import AutomationScheduler
    monkeypatch.setattr("core.services.user_model_selection.resolve_effective_chat_model_name", lambda: "test")
    monkeypatch.setattr("core.services.ontology_service.build_user_ontology_runtime", lambda **kw: (False, {}))
    captured = []
    async def workflow(**kwargs):
        captured.append(kwargs["context"])
        yield {"type": "content", "delta": "report complete"}
    monkeypatch.setattr("orchestration.workflow.astream_chat_workflow", workflow)
    with factory() as db:
        task = AutomationService(db).create_task(
            user_id="owner", task_type="prompt", prompt="summarize project",
            cron_expression="0 18 * * *", execution_location="local", project_id="local-project")
        task_id = task.task_id
    chat_id, result, _ = await AutomationScheduler()._execute_prompt_task(
        user_id="owner", task_name="report", prompt="summarize project", task_id=task_id,
        enabled_mcp_ids=[], enabled_skill_ids=[], enabled_kb_ids=[])
    assert result == "report complete"
    assert captured[0]["project_id"] == "local-project"
    assert captured[0]["project_local_path"] == str(path)
    assert captured[0]["project_instructions"] == "Summarize changes"
    assert captured[0]["run_id"] and captured[0]["journal_owner"]
    from core.db.models import ChatSession
    with factory() as db:
        assert db.get(ChatSession, chat_id).project_id == "local-project"


@pytest.mark.asyncio
async def test_local_conversation_creation_uses_authorized_project(local_project, desktop_identity):
    import json
    from types import SimpleNamespace
    from core.db.models import ChatSession
    from core.services.run_journal import RunJournal
    from core.services.tool_effect_ledger import ToolEffectJournal, CURRENT_TOOL_EFFECT
    from core.services.automation_tool_routing import creation_arguments, local_automation_result
    factory, _ = local_project
    with factory() as db:
        db.add(ChatSession(chat_id="bound-chat", user_id="owner", title="report", project_id="local-project"))
        db.commit()
    journal = RunJournal(factory)
    journal.accept(run_id="bound-run", message_id="bound-msg", chat_id="bound-chat",
                   user_id="owner", request_payload={}, recovery_snapshot={})
    assert journal.claim("bound-run", owner="worker", lease_seconds=300)
    args = creation_arguments({"cron_expression": "0 18 * * *", "prompt": "report"}, "owner", "bound-chat")
    assert args["execution_location"] == "local" and args["project_id"] == "local-project"
    intent = ToolEffectJournal(factory).begin_intent(run_id="bound-run", owner="worker",
        claim_owner="call", tool_call_id="call", tool_name="create_scheduled_task",
        args=args, recovery_policy="reconcile").intent
    async def authorize():
        return None
    token = CURRENT_TOOL_EFFECT.set(SimpleNamespace(effect_id=intent.effect_id, run_id="bound-run"))
    try:
        result = await local_automation_result("automation", "create_scheduled_task",
                    {**args, "tool_effect_id": intent.effect_id}, {"x-chat-id": "bound-chat"}, authorize=authorize)
        data = json.loads(result.content[0].text)
        assert data["ok"] and data["task"]["execution_location"] == "local"
        assert data["task"]["project_id"] == "local-project"
        async def revoked():
            raise ValueError("authorization revoked")
        with pytest.raises(ValueError, match="revoked"):
            await local_automation_result("automation", "delete_scheduled_task",
                {"task_ref": data["task"]["task_id"], "execution_location": "local"},
                {"x-chat-id": "bound-chat"}, authorize=revoked)
        with factory() as db:
            from core.db.models import ScheduledTask
            assert db.get(ScheduledTask, data["task"]["task_id"]) is not None
        desktop_identity("account-b", "cloud-other")
        with pytest.raises(ValueError, match="账号"):
            await local_automation_result("automation", "create_scheduled_task",
                    {**args, "tool_effect_id": intent.effect_id}, {"x-chat-id": "bound-chat"}, authorize=authorize)
    finally:
        CURRENT_TOOL_EFFECT.reset(token)


def test_public_urls_are_not_host_paths():
    from core.services.automation_execution import references_host_path
    assert not references_host_path("Summarize https://example.com/news")
    assert references_host_path(r"Read C:\Users\Aaron")
    assert references_host_path("Read /home/aaron/project")


@pytest.fixture
def local_chat(local_project, desktop_identity):
    from core.db.models import ChatSession
    factory, _ = local_project
    with factory() as db:
        db.add(ChatSession(chat_id="local-chat", user_id="owner",
                           title="report", project_id="local-project"))
        db.commit()
    return desktop_identity


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["switch", "logout", "server"])
async def test_local_task_rejects_identity_change_during_authorization(local_chat, change):
    from core.capabilities.errors import CloudUnavailable
    from core.services import desktop_cloud_bridge as bridge
    from core.services.automation_tool_routing import local_automation_result
    from mcp_servers.automation_task_mcp import impl

    async def authorize():
        if change == "switch":
            local_chat("account-b", "cloud-other")
        elif change == "server":
            local_chat(cloud_base="https://other.example.com")
        else:
            bridge._state = None

    with pytest.raises(CloudUnavailable):
        await local_automation_result(
            "automation", "create_scheduled_task",
            {"cron_expression": "0 * * * *", "prompt": "report"},
            {"x-chat-id": "local-chat"}, authorize=authorize)
    assert impl.list_tasks(user_id="owner")["count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [
    "create_scheduled_task", "list_scheduled_tasks", "get_scheduled_task",
    "update_scheduled_task", "pause_scheduled_task", "resume_scheduled_task",
    "delete_scheduled_task",
])
@pytest.mark.parametrize("identity", ["other", "missing", "other_server"])
async def test_local_task_tools_reject_unmapped_or_other_account(local_chat, tool, identity):
    from core.services.automation_tool_routing import local_automation_result
    if identity == "other":
        local_chat("account-b", "cloud-other")
    elif identity == "missing":
        # Even an equal raw subject cannot substitute for an account mapping.
        local_chat("missing-account", "owner")
    else:
        local_chat(cloud_base="https://other.example.com")

    async def authorize():
        pytest.fail("Unrelated account must be rejected before tool authorization")

    with pytest.raises(ValueError, match="账号"):
        await local_automation_result(
            "automation", tool, {"execution_location": "local"},
            {"x-chat-id": "local-chat"}, authorize=authorize)


@pytest.mark.asyncio
async def test_mapped_account_can_manage_local_tasks(local_chat):
    import json
    from core.services.automation_tool_routing import local_automation_result

    async def authorize():
        return None

    async def call(tool, **arguments):
        result = await local_automation_result(
            "automation", tool, {"execution_location": "local", **arguments},
            {"x-chat-id": "local-chat"}, authorize=authorize)
        data = json.loads(result.content[0].text)
        assert data["ok"], data
        return data

    created = await call("create_scheduled_task", cron_expression="0 * * * *", prompt="report")
    ref = created["task"]["task_id"]
    assert (await call("list_scheduled_tasks"))["count"] == 1
    assert (await call("get_scheduled_task", task_ref=ref))["task"]["task_id"] == ref
    assert (await call("update_scheduled_task", task_ref=ref, name="updated"))["task"]["name"] == "updated"
    await call("pause_scheduled_task", task_ref=ref)
    await call("resume_scheduled_task", task_ref=ref)
    await call("delete_scheduled_task", task_ref=ref)
    assert (await call("list_scheduled_tasks"))["count"] == 0


@pytest.mark.asyncio
async def test_local_task_rejects_account_switch_before_queued_dispatch(local_chat, monkeypatch):
    import asyncio
    from core.capabilities.errors import CloudUnavailable
    from core.services.automation_tool_routing import local_automation_result
    from mcp_servers.automation_task_mcp import impl

    original = asyncio.to_thread

    async def queued_dispatch(function, *args, **kwargs):
        local_chat("account-b", "cloud-other")
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", queued_dispatch)

    async def authorize():
        return None

    with pytest.raises(CloudUnavailable):
        await local_automation_result(
            "automation", "create_scheduled_task",
            {"cron_expression": "0 * * * *", "prompt": "report"},
            {"x-chat-id": "local-chat"}, authorize=authorize)
    assert impl.list_tasks(user_id="owner")["count"] == 0
