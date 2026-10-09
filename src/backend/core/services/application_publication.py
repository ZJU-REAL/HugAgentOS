"""Coordinate hosted publication and its private MCP connection across stores.

The stores have independent transactions. Persist credentials disabled before the
protocol probe; only enable after verification. A failed registration is reported
as a partial result and a retry repairs the deterministic connection.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path

from core.db.models import AdminMcpServer
from core.infra.time import utc_now
from core.services.application_deployments import ApplicationDeploymentService
from core.services.application_store import mcp_deployments, owned_application
from fastapi import HTTPException
from sqlalchemy import select, text

logger = logging.getLogger(__name__)


@contextmanager
def publication_lock(engine, app_id):
    if not re.fullmatch(r"[0-9a-f]{32}", app_id):
        raise HTTPException(404, "Application not found")
    if engine.dialect.name == "sqlite":
        from core.capabilities.lockfile import LockTimeout, locked

        try:
            with locked(Path(engine.url.database + ".mcp-publication.lock")):
                yield
        except LockTimeout:
            raise HTTPException(409, "Another MCP publication is in progress")
        return
    key = int.from_bytes(
        hashlib.sha256(("hosted-mcp:" + app_id).encode()).digest()[:8], "big", signed=True
    )
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        deadline = time.monotonic() + 10
        while not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
            if time.monotonic() >= deadline:
                raise HTTPException(409, "Another MCP publication is in progress")
            time.sleep(0.05)
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


class ApplicationPublicationService:
    def __init__(self, db, engine=None):
        self.db = db
        self.deployment = ApplicationDeploymentService(engine)
        self.engine = self.deployment.engine

    def _connection(self, app_id, owner):
        self.db.expire_all()
        row = self.db.get(AdminMcpServer, "amcp_" + app_id)
        if row and (
            row.owner_user_id != owner or (row.extra_config or {}).get("hosted_app_id") != app_id
        ):
            raise HTTPException(409, "Personal MCP identifier is already in use")
        return row

    def publish(
        self, app_id, owner, tools=None, *, install_personal=False, from_project=False, chat_id=""
    ):
        from core.auth.capabilities import resolve_user_capabilities
        from core.config.settings import settings
        from core.services import mcp_management_service as management

        with publication_lock(self.engine, app_id):
            with self.engine.connect() as connection:
                app = owned_application(connection, app_id, owner)
            if install_personal and not resolve_user_capabilities(self.db, owner).get(
                "can_add_mcp"
            ):
                raise HTTPException(403, "Administrator has not enabled personal MCP creation")
            from core.services.application_projects import ApplicationProjectService
            from core.services.application_schema import MCPProjectDefinition
            from pydantic import ValidationError

            projects = ApplicationProjectService(self.db, self.engine)
            project, app = projects.ensure(app_id, owner)
            projects.bind_chat(project, owner, chat_id)
            source = projects.source(project, owner)
            if not from_project and projects.decode_draft(
                source["content"]
            ) != projects.decode_draft(projects.encode(app)):
                raise HTTPException(
                    409,
                    "Project has unpublished MCP changes; publish from the project or reconcile its draft first",
                )
            if from_project:
                if len(source["content"].encode()) > 512 * 1024:
                    raise HTTPException(413, "MCP definition exceeds 512 KiB")
                try:
                    definition = MCPProjectDefinition.model_validate_json(source["content"])
                except ValidationError:
                    raise HTTPException(
                        422, "Invalid mcp.json; preserve app_id, version and valid tools"
                    )
                if definition.app_id != app_id:
                    raise HTTPException(409, "MCP definition belongs to another application")
                if definition.version != app["mcp_version"]:
                    raise HTTPException(
                        409, "MCP definition is stale; reconcile with the current published version"
                    )
                tools = definition.tools
            row = self._connection(app_id, owner)
            enable = install_personal or bool(row and row.is_enabled)
            receipt = self.deployment.publish_mcp(app_id, owner, tools)
            receipt["app_id"] = app_id
            receipt["published"] = True
            receipt["project_id"] = project.project_id
            try:
                projects.sync(project, owner, app_id, source["revision"])
                receipt["project_synced"] = True
            except Exception as error:
                self.db.rollback()
                logger.warning(
                    "MCP source synchronization failed app=%s version=%s error_type=%s",
                    app_id,
                    receipt["version"],
                    type(error).__name__,
                )
                receipt["project_synced"] = False
            if row is None and not install_personal:
                return receipt
            try:
                if row is None:
                    row = AdminMcpServer(
                        server_id="amcp_" + app_id,
                        owner_user_id=owner,
                        created_by=owner,
                        created_at=utc_now(),
                        is_stable=False,
                    )
                    self.db.add(row)
                row.display_name = app["title"]
                row.description = "Hosted application query MCP"
                row.transport = "streamable_http"
                # Runtime executes in this backend; no caller-controlled URL or headers.
                row.url = f"http://127.0.0.1:{settings.server.port}/applications-mcp/{app_id}"
                row.headers = management.encrypt_mcp_headers(
                    {"Authorization": "Bearer " + receipt["token"]}
                )
                row.is_enabled = False
                row.tools_json = []
                row.extra_config = {
                    **(row.extra_config or {}),
                    "hosted_app_id": app_id,
                    "hosted_mcp_version": receipt["version"],
                }
                row.updated_at = utc_now()
                self.db.commit()
                management.refresh_mcp_caches()
                ok, _ = asyncio.run(
                    management.probe_mcp_connectivity(row, self.db, timeout_seconds=15)
                )
                if ok:
                    row.is_enabled = enable
                    self.db.commit()
                else:
                    self.db.rollback()
                receipt.update(
                    server_id=row.server_id,
                    installed=ok,
                    enabled=ok and enable,
                    connection_verified=ok,
                )
                if not ok:
                    receipt["message"] = (
                        "MCP published; connection verification failed. Retry publication to repair."
                    )
            except Exception as error:
                logger.warning(
                    "Personal MCP registration failed app=%s version=%s error_type=%s",
                    app_id,
                    receipt["version"],
                    type(error).__name__,
                )
                self.db.rollback()
                # No credential is returned to the conversation, including partial failures.
                receipt.update(
                    installed=False,
                    enabled=False,
                    connection_verified=False,
                    message="MCP published; personal registration failed. Retry publication to repair.",
                )
            finally:
                management.refresh_mcp_caches()
            if install_personal:
                receipt.pop("token", None)
            return receipt

    def revoke(self, app_id, owner):
        from core.services.mcp_management_service import refresh_mcp_caches

        with publication_lock(self.engine, app_id):
            with self.engine.connect() as connection:
                owned_application(connection, app_id, owner)
            row = self._connection(app_id, owner)
            result = self.deployment.revoke(app_id, owner)
            if row:
                row.is_enabled = False
                row.headers = {}
                row.updated_at = utc_now()
                self.db.commit()
                refresh_mcp_caches()
            return result

    def rollback(self, app_id, owner, version):
        from core.services.application_schema import ToolDefinition

        with self.engine.connect() as connection:
            owned_application(connection, app_id, owner)
            tools = connection.scalar(
                select(mcp_deployments.c.tools).where(
                    mcp_deployments.c.app_id == app_id, mcp_deployments.c.version == version
                )
            )
        if tools is None:
            raise HTTPException(404, "MCP deployment not found")
        return self.publish(app_id, owner, [ToolDefinition.model_validate(tool) for tool in tools])


async def finish_application_publication(owner, receipt):
    """Return only after the mounted project reflects its committed new version.

    A failed mirror refresh is partial completion: publication already committed,
    so preserve the receipt and never turn it into an ambiguous HTTP failure.
    """
    if receipt.get("project_synced"):
        from core.space_sync.personal_registry import flush_user

        try:
            await flush_user(owner)
        except HTTPException:
            receipt["project_synced"] = False
            logger.warning(
                "MCP project refresh incomplete app=%s version=%s",
                receipt.get("app_id"),
                receipt.get("version"),
            )
    return receipt
