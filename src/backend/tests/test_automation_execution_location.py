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
async def test_local_conversation_creation_uses_authorized_project(local_project, monkeypatch):
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
    monkeypatch.setattr("core.services.desktop_cloud_bridge.ensure_current_authorization", lambda *a: None)
    monkeypatch.setattr("core.services.desktop_cloud_bridge.get_state", lambda: {"token": "fixture"})
    monkeypatch.setattr("core.services.desktop_capability_protocol.token_subject", lambda token: "owner")
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
        monkeypatch.setattr("core.services.desktop_capability_protocol.token_subject", lambda token: "other")
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
