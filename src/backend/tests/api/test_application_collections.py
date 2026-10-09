"""Public collection migration behavior and structured survey columns."""

import pytest
from core.services.application_data import ApplicationDataService
from core.services.application_schema import ApplicationDefinition, RecordBatch, TableDefinition
from fastapi import HTTPException
from tests.api.test_application_hosting import hosted


def test_public_collection_compare_and_swap(hosted):
    _, engine = hosted
    service = ApplicationDataService(engine)
    app = service.create("owner", ApplicationDefinition(title="Tasks"))
    definition = TableDefinition(
        name="tasks",
        public_read=True,
        public_replace=True,
        columns=[{"name": "task_id", "unique": True, "required": True}, {"name": "title"}],
    )
    service.define_table(app["id"], "owner", definition)
    from core.services.application_collections import read_collection, replace_collection

    original = read_collection(service, app["id"], "owner", "tasks")
    updated = replace_collection(
        service,
        app["id"],
        "owner",
        "tasks",
        original["revision"],
        [{"task_id": "one", "title": "First"}],
    )
    assert updated["items"][0]["title"] == "First"
    with pytest.raises(HTTPException) as error:
        replace_collection(service, app["id"], "owner", "tasks", original["revision"], [])
    assert error.value.status_code == 409
    assert read_collection(service, app["id"], "owner", "tasks") == updated
    emptied = replace_collection(service, app["id"], "owner", "tasks", updated["revision"], [])
    assert emptied["items"] == []


def test_json_survey_and_private_read(hosted):
    _, engine = hosted
    service = ApplicationDataService(engine)
    app = service.create("owner", ApplicationDefinition(title="Survey"))
    service.define_table(
        app["id"],
        "owner",
        TableDefinition(
            name="responses", public_insert=True, columns=[{"name": "ratings", "type": "json"}]
        ),
    )
    service.insert(app["id"], "owner", "responses", RecordBatch(rows=[{"ratings": {"a": 4}}]))
    assert service.query(app["id"], "owner", "responses")["items"][0]["ratings"] == {"a": 4}
    from core.services.application_collections import read_collection

    with pytest.raises(HTTPException) as error:
        read_collection(service, app["id"], "owner", "responses")
    assert error.value.status_code == 404


@pytest.mark.parametrize("rows", [[None], [1], [["title"]], [{"title": float("nan")}]])
def test_collection_rejects_invalid_rows(hosted, rows):
    _, engine = hosted
    from core.services.application_collections import replace_collection

    with pytest.raises(HTTPException) as error:
        replace_collection(
            ApplicationDataService(engine), "a" * 32, "owner", "tasks", "a" * 64, rows
        )
    assert error.value.status_code == 422


def test_json_constraints_and_mcp_filters(hosted):
    from core.services.application_deployments import ApplicationDeploymentService
    from core.services.application_schema import ColumnDefinition, ToolDefinition
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ColumnDefinition(name="ratings", type="json", unique=True)
    _, engine = hosted
    service = ApplicationDataService(engine)
    app = service.create("owner", ApplicationDefinition(title="Survey"))
    service.define_table(
        app["id"],
        "owner",
        TableDefinition(name="responses", columns=[{"name": "ratings", "type": "json"}]),
    )
    with pytest.raises(HTTPException) as error:
        ApplicationDeploymentService(engine).publish_mcp(
            app["id"],
            "owner",
            [
                ToolDefinition(
                    name="query",
                    description="Query",
                    table="responses",
                    fields=["ratings"],
                    filters=["ratings"],
                )
            ],
        )
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as query_error:
        service.query(app["id"], "owner", "responses", filters={"ratings": {"value": 1}})
    assert query_error.value.status_code == 422


@pytest.mark.parametrize("reverse", [False, True])
def test_collection_optional_fields_preserved(hosted, reverse):
    _, engine = hosted
    service = ApplicationDataService(engine)
    app = service.create("owner", ApplicationDefinition(title="Optional fields"))
    service.define_table(
        app["id"],
        "owner",
        TableDefinition(
            name="tasks",
            public_read=True,
            public_replace=True,
            columns=[{"name": "task_id"}, {"name": "title"}],
        ),
    )
    from core.services.application_collections import read_collection, replace_collection

    original = read_collection(service, app["id"], "owner", "tasks")
    rows = [{"task_id": "one", "title": "Preserved"}, {"task_id": "two"}]
    result = replace_collection(
        service,
        app["id"],
        "owner",
        "tasks",
        original["revision"],
        list(reversed(rows)) if reverse else rows,
    )
    assert {row["task_id"]: row["title"] for row in result["items"]} == {
        "one": "Preserved",
        "two": None,
    }


def test_public_data_route_precedes_static_site_catch_all():
    from api.app import app

    paths = [route.path for route in app.routes if hasattr(route, "path")]
    assert paths.index("/site/{slug}/__api/data/{table}") < paths.index(
        "/site/{slug}/{path:path}"
    )
