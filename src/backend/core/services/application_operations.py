"""Validated application actions shared by the agent callback and owner creation."""

from core.services.application_data import ApplicationDataService
from core.services.application_publication import ApplicationPublicationService
from core.services.application_schema import (
    ApplicationDefinition,
    InsertOperation,
    InternalOperation,
    MCPPublish,
    QueryOperation,
    TableDefinition,
)
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session


def create_application(
    definition: ApplicationDefinition, owner: str, db: Session, chat_id: str = ""
) -> dict:
    if definition.site_id:
        from core.services.site_service import SiteService

        SiteService(db).get_owned(definition.site_id, owner, required="admin")
    service = ApplicationDataService()
    app = service.create(owner, definition)
    if definition.kind == "mcp":
        from core.services.application_projects import ApplicationProjectService

        editor = ApplicationProjectService(db, service.engine).editor(app["id"], owner, chat_id)
        app["project_id"] = editor["project_id"]
    return app


def perform_operation(body: InternalOperation, db: Session) -> dict:
    service = ApplicationDataService()
    payload = body.payload
    try:
        if body.action == "list":
            return {"items": service.list(body.user_id)}
        if body.action == "create":
            return create_application(
                ApplicationDefinition.model_validate(payload), body.user_id, db, body.chat_id
            )
        if body.action == "source":
            from core.services.application_projects import ApplicationProjectService

            return ApplicationProjectService(db, service.engine).editor(
                body.app_id, body.user_id, body.chat_id
            )
        if body.action == "publish_project":
            return ApplicationPublicationService(db, service.engine).publish(
                body.app_id,
                body.user_id,
                install_personal=True,
                from_project=True,
                chat_id=body.chat_id,
            )
        if body.action == "table":
            return service.define_table(
                body.app_id, body.user_id, TableDefinition.model_validate(payload)
            )
        if body.action == "insert":
            batch = InsertOperation.model_validate(payload)
            return service.insert(body.app_id, body.user_id, batch.table, batch)
        if body.action == "query":
            query = QueryOperation.model_validate(payload)
            return service.query(
                body.app_id,
                body.user_id,
                query.table,
                filters=query.filters,
                limit=query.limit,
                offset=query.offset,
            )
        if body.action == "publish_mcp":
            return ApplicationPublicationService(db, service.engine).publish(
                body.app_id,
                body.user_id,
                MCPPublish.model_validate(payload).tools,
                install_personal=True,
                chat_id=body.chat_id,
            )
        if body.action == "revoke_mcp":
            return ApplicationPublicationService(db, service.engine).revoke(
                body.app_id, body.user_id
            )
        if body.action == "import_industry":
            from core.services.application_industry import import_enterprises

            return import_enterprises(service, db, body.app_id, body.user_id, payload)
    except ValidationError as error:
        raise HTTPException(
            422,
            {
                "message": "Invalid application operation definition",
                "errors": [
                    {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
                    for item in error.errors(
                        include_input=False, include_context=False, include_url=False
                    )
                ],
            },
        )
    raise HTTPException(422, "Unknown application operation")
