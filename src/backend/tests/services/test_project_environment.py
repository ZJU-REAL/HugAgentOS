from core.capabilities.skills import loader_factory as loader_factory_module
import pytest


@pytest.fixture(autouse=True)
def inventory_database(db_session, monkeypatch):
    from core.capabilities.skills import loader
    from core.db import engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(engine, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(loader_factory_module, 'global_loader', None)


from core.db.models import Project, UserShadow
from core.projects.environment.service import ProjectEnvironmentService


def test_empty_custom_environment_is_distinct_from_all_and_persists(db_session):
    db_session.add(UserShadow(user_id="env-owner", username="Owner"))
    project = Project(
        project_id="env-project",
        name="Environment",
        kind="personal",
        owner_user_id="env-owner",
        extra_data={"memory_enabled": False},
    )
    db_session.add(project)
    db_session.commit()
    service = ProjectEnvironmentService(db_session)
    assert service.read(project)["mode"] == "all"
    service.save(project, "env-owner", {"mode": "custom"})
    db_session.expire_all()
    result = service.read(project)
    assert result == {
        "mode": "custom",
        "agent_ids": [],
        "plugin_ids": [],
        "skill_ids": [],
        "mcp_ids": [],
    }
    assert project.extra_data["memory_enabled"] is False


def test_dependencies_are_expanded_without_granting_missing_resources():
    from core.projects.environment.dependencies import expand_environment

    graph = {
        "agent_ids": {"analyst": {"name": "Analyst", "dependencies": {"plugin_ids": ["charts"]}}},
        "plugin_ids": {"charts": {"name": "Charts", "dependencies": {"skill_ids": ["plot"]}}},
        "skill_ids": {"plot": {"name": "Plot", "dependencies": {"mcp_ids": ["data", "foreign"]}}},
        "mcp_ids": {"data": {"name": "Data", "dependencies": {}}},
    }
    resolved = expand_environment({"mode": "custom", "agent_ids": ["analyst"]}, graph)
    assert resolved["effective"] == {
        "agent_ids": ["analyst"],
        "plugin_ids": ["charts"],
        "skill_ids": ["plot"],
        "mcp_ids": ["data"],
    }
    assert [d["id"] for d in resolved["dependencies"]] == ["charts", "plot", "data"]
    assert resolved["unavailable"] == [{"kind": "mcp_ids", "id": "foreign", "required_by": "plot"}]


def test_binding_private_connector_cannot_grant_other_user_access(db_session):
    import pytest
    from core.db.models import AdminMcpServer
    from core.projects.environment_resources import environment_resources
    from fastapi import HTTPException

    db_session.add_all(
        [UserShadow(user_id="alice", username="Alice"), UserShadow(user_id="bob", username="Bob")]
    )
    db_session.add(
        AdminMcpServer(
            server_id="bob-data",
            display_name="Bob data",
            owner_user_id="bob",
            is_enabled=True,
            transport="streamable_http",
            url="http://connector.invalid/mcp",
        )
    )
    db_session.commit()
    service = ProjectEnvironmentService(db_session)
    with pytest.raises(HTTPException) as error:
        service.validate("alice", {"mode": "custom", "mcp_ids": ["bob-data"]})
    assert error.value.status_code == 422
    graph = environment_resources(db_session, "bob")
    assert "bob-data" in graph["mcp_ids"]
    assert service.validate("bob", {"mode": "custom", "mcp_ids": ["bob-data"]})["mcp_ids"] == [
        "bob-data"
    ]


def test_environment_is_saved_in_the_same_project_creation_transaction(db_session):
    from core.projects.edition import ProjectService

    db_session.add(UserShadow(user_id="creator", username="Creator"))
    db_session.commit()
    service = ProjectEnvironmentService(db_session)
    with service.creation_metadata("creator", {"mode": "custom"}):
        project = ProjectService(db_session).create_personal("creator", "Created with environment")
    db_session.expire_all()
    assert service.read(project)["mode"] == "custom"
    # The listener is request-scoped and must not affect later project creation.
    ordinary = ProjectService(db_session).create_personal("creator", "Ordinary")
    assert service.read(ordinary)["mode"] == "all"


def test_deep_dependency_cycles_resolve_without_recursion_or_duplicates():
    from core.projects.environment.dependencies import expand_environment

    graph = {field: {} for field in ("agent_ids", "plugin_ids", "skill_ids", "mcp_ids")}
    for index in range(1200):
        graph["mcp_ids"][str(index)] = {
            "name": str(index),
            "dependencies": {"mcp_ids": [str((index + 1) % 1200)]},
        }
    result = expand_environment({"mode": "custom", "mcp_ids": ["0"]}, graph)
    assert result["effective"]["mcp_ids"] == [str(index) for index in range(1200)]
    assert len(result["dependencies"]) == 1199
    assert result["unavailable"] == []


def test_configuration_normalizes_identifiers_without_http_dependencies():
    from core.projects.environment.config import ProjectEnvironment
    from pydantic import ValidationError

    config = ProjectEnvironment(mode="custom", skill_ids=[" report ", "report"])
    assert config.skill_ids == ["report"]
    assert ProjectEnvironment(mode="all", skill_ids=["report"]).skill_ids == []
    with pytest.raises(ValidationError):
        ProjectEnvironment(mode="custom", skill_ids=[" "])
