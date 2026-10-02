"""Local project delivery uses the original file through the public tools/API."""

from types import SimpleNamespace

import pytest


from sqlalchemy.orm import sessionmaker

from core.db.models import Project, UserShadow, ChatSession

from core.llm import workspace

from core.llm.tool_collector import ToolCollector

from core.llm.tools.pin_tool import register_pin_to_workspace

from core.services.project_scope import ProjectScope


@pytest.fixture
def local_project(tmp_path, db_session, monkeypatch):
    monkeypatch.setenv("HUGAGENT_LOCAL_MODE", "1")
    from core.config import local_mode

    monkeypatch.setattr(local_mode, "local_mode_enabled", lambda: True)
    from api.routes import files

    monkeypatch.setattr(files, "local_mode_enabled", lambda: True)
    from core.artifacts import store

    monkeypatch.setattr(store, "_STORE_DIR", tmp_path / "artifacts")
    monkeypatch.setattr("core.db.engine.SessionLocal", sessionmaker(bind=db_session.get_bind()))
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("HUGAGENT_HOME", str(tmp_path / "home"))
    from core.services.local_grant_service import add_grant

    add_grant(str(root))
    source = root / "report.txt"
    source.write_text("original report")
    db_session.add(UserShadow(user_id="pin-user", username="Pin user"))
    db_session.add(
        Project(
            project_id="pin-project",
            name="Reports",
            kind="local",
            owner_user_id="pin-user",
            extra_data={"local": {"path": str(root), "slug": "reports"}},
        )
    )
    db_session.add(ChatSession(chat_id="pin-chat", user_id="pin-user", project_id="pin-project"))
    db_session.commit()
    from core.infra.logging import user_id_var, chat_id_var

    token = user_id_var.set("pin-user")
    chat_token = chat_id_var.set("")
    scope = ProjectScope(
        project_id="pin-project",
        kind="local",
        root_folder_id="",
        folder_name="reports",
        is_local=True,
        local_slug="reports",
        local_path=str(root),
    )
    from core.llm.tool_permissions import (
        CURRENT_PERMISSION_TICKET,
        PermissionTicket,
        PermissionIntent,
    )

    ticket_token = CURRENT_PERMISSION_TICKET.set(
        PermissionTicket(
            "sandbox_get_artifact",
            "export",
            "",
            "test-export",
            (PermissionIntent("local_path", "read", str(source), "export original"),),
        )
    )
    workspace.init_state()
    yield source, scope
    CURRENT_PERMISSION_TICKET.reset(ticket_token)
    user_id_var.reset(token)
    chat_id_var.reset(chat_token)


def pin_tool(scope, session_id=None):
    collector = ToolCollector()
    register_pin_to_workspace(collector, scope=scope, sandbox_session_id=session_id)

    async def invoke(**args):
        from core.llm.tool_permissions import (
            ToolPermissionRegistry,
            ToolPermissionService,
            PermissionRuntime,
            CURRENT_PERMISSION_TICKET,
        )

        registry = ToolPermissionRegistry()
        registry.register(
            "pin_to_workspace", collector.permission_specs["pin_to_workspace"], source="test"
        )
        service = ToolPermissionService(
            registry,
            PermissionRuntime(
                chat_id="pin-chat", user_id="pin-user", interactive=False, approval_available=False
            ),
        )
        outcome = await service.authorize(
            SimpleNamespace(name="pin_to_workspace", id="pin-call", input=args)
        )
        if not outcome.proceed:
            raise PermissionError(str(outcome.payload))
        token = CURRENT_PERMISSION_TICKET.set(outcome.ticket)
        try:
            return await collector.get_tool("pin_to_workspace")._func(**args)
        finally:
            CURRENT_PERMISSION_TICKET.reset(token)

    return invoke
