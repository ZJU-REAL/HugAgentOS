"""Owner-managed application databases and hosted MCP deployments."""

from __future__ import annotations

import csv
import io
import json

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.responses import success_response
from core.services.application_data import ApplicationDataService
from core.services.application_operations import create_application
from core.services.application_publication import (
    ApplicationPublicationService,
    finish_application_publication,
)
from core.services.application_schema import (
    ApplicationDefinition,
    MCPPublish,
    MCPRollback,
    PatchRecord,
    RecordBatch,
    TableDefinition,
)
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/v1/applications", tags=["Applications"])


@router.get("")
def list_applications(user: UserContext = Depends(get_current_user)):
    from core.config.application_hosting import application_hosting_settings
    from core.config.settings import settings

    if not application_hosting_settings.database_url and not settings.deploy.is_local:
        return success_response(data={"items": [], "available": False})
    return success_response(
        data={"items": ApplicationDataService().list(user.user_id), "available": True}
    )


@router.post("")
def create(
    body: ApplicationDefinition,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return success_response(data=create_application(body, user.user_id, db))


@router.get("/{app_id}")
def detail(app_id: str, user: UserContext = Depends(get_current_user)):
    return success_response(data=ApplicationDataService().get(app_id, user.user_id))


@router.post("/{app_id}/editor")
async def open_editor(
    app_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from core.services.application_projects import ApplicationProjectService

    service = ApplicationDataService()
    projects = ApplicationProjectService(db, service.engine)
    await projects.flush_source(app_id, user.user_id)
    return success_response(data=await run_in_threadpool(projects.editor, app_id, user.user_id))


@router.post("/{app_id}/tables")
def define_table(app_id: str, body: TableDefinition, user: UserContext = Depends(get_current_user)):
    return success_response(data=ApplicationDataService().define_table(app_id, user.user_id, body))


@router.post("/{app_id}/tables/{table}/records")
def insert_records(
    app_id: str, table: str, body: RecordBatch, user: UserContext = Depends(get_current_user)
):
    return success_response(data=ApplicationDataService().insert(app_id, user.user_id, table, body))


@router.get("/{app_id}/tables/{table}/records")
def query_records(
    app_id: str,
    table: str,
    filters: str = "{}",
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    user: UserContext = Depends(get_current_user),
):
    try:
        values = json.loads(filters)
    except ValueError:
        raise HTTPException(422, "Invalid filters")
    if not isinstance(values, dict):
        raise HTTPException(422, "Filters must be an object")
    return success_response(
        data=ApplicationDataService().query(
            app_id,
            user.user_id,
            table,
            filters=values,
            limit=limit,
            offset=offset,
        )
    )


@router.patch("/{app_id}/tables/{table}/records/{record_id}")
def patch_record(
    app_id: str,
    table: str,
    record_id: str,
    body: PatchRecord,
    user: UserContext = Depends(get_current_user),
):
    return success_response(
        data=ApplicationDataService().patch(
            app_id,
            user.user_id,
            table,
            record_id,
            body.version,
            body.values,
        )
    )


@router.get("/{app_id}/tables/{table}/export")
def export_records(
    app_id: str,
    table: str,
    offset: int = Query(0, ge=0, le=100000),
    user: UserContext = Depends(get_current_user),
):
    result = ApplicationDataService().query(app_id, user.user_id, table, limit=100, offset=offset)
    buffer = io.StringIO()
    if result["items"]:
        writer = csv.DictWriter(buffer, fieldnames=list(result["items"][0]))
        writer.writeheader()
        for row in result["items"]:
            # Spreadsheet formula injection protection for downloaded untrusted text.
            writer.writerow(
                {
                    k: (
                        "'" + v
                        if isinstance(v, str)
                        and (
                            v.startswith(("\t", "\r", "\n"))
                            or v.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠"))
                        )
                        else v
                    )
                    for k, v in row.items()
                }
            )
    return Response(
        buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="records.csv"',
            "X-Total-Count": str(result["total"]),
            "Cache-Control": "no-store",
        },
    )


@router.post("/{app_id}/mcp")
async def publish_mcp(
    app_id: str,
    body: MCPPublish,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from core.services.application_projects import ApplicationProjectService

    publication = ApplicationPublicationService(db)
    await ApplicationProjectService(db, publication.engine).flush_source(app_id, user.user_id)
    receipt = await run_in_threadpool(publication.publish, app_id, user.user_id, body.tools)
    return success_response(data=await finish_application_publication(user.user_id, receipt))


@router.post("/{app_id}/mcp/project")
async def publish_project_mcp(
    app_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from core.services.application_projects import ApplicationProjectService

    publication = ApplicationPublicationService(db)
    await ApplicationProjectService(db, publication.engine).flush_source(app_id, user.user_id)
    receipt = await run_in_threadpool(publication.publish, app_id, user.user_id, from_project=True)
    return success_response(data=await finish_application_publication(user.user_id, receipt))


@router.delete("/{app_id}/mcp")
def revoke_mcp(
    app_id: str, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)
):
    return success_response(data=ApplicationPublicationService(db).revoke(app_id, user.user_id))


@router.post("/{app_id}/mcp/rollback")
async def rollback_mcp(
    app_id: str,
    body: MCPRollback,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from core.services.application_projects import ApplicationProjectService

    publication = ApplicationPublicationService(db)
    await ApplicationProjectService(db, publication.engine).flush_source(app_id, user.user_id)
    receipt = await run_in_threadpool(publication.rollback, app_id, user.user_id, body.version)
    return success_response(data=await finish_application_publication(user.user_id, receipt))
