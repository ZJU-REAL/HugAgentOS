"""Idempotent source-project provisioning for hosted declarative MCPs.

Application and platform stores cannot commit together. Deterministic project
identity permits recovery after either commit, and the publication lock serializes
provisioning with publication. Source files use the existing revision-aware store.
"""

from __future__ import annotations

import json

from core.auth.permissions_iface import resolve_project_permission
from core.db.models import ChatSession, Project, UserFolder
from core.infra.time import utc_now
from core.services.application_store import application_sources, owned_application
from core.services.project_source import ProjectSourceService
from fastapi import HTTPException
from sqlalchemy import insert, select

SOURCE_PATH = "mcp.json"


class ApplicationProjectService:
    def __init__(self, db, engine):
        self.db, self.engine = db, engine

    async def flush_source(self, app_id, owner):
        from core.space_sync.personal_registry import flush_user
        from starlette.concurrency import run_in_threadpool

        paths = await run_in_threadpool(self._source_paths, app_id, owner)
        self.db.rollback()  # Release the read transaction before the registry writes.
        if paths:
            await flush_user(owner, metadata_only=True, paths=tuple(paths))

    def _source_paths(self, app_id, owner):
        with self.engine.connect() as connection:
            owned_application(connection, app_id, owner)
            project_id = connection.scalar(
                select(application_sources.c.project_id).where(
                    application_sources.c.app_id == app_id,
                )
            )
        if not project_id:
            return []
        project = ProjectSourceService(self.db).authorized_project(project_id, owner, write=False)
        if resolve_project_permission(self.db, owner, project) not in ("edit", "admin"):
            raise HTTPException(403, "MCP source project is not editable")
        if project.kind != "personal":
            return []  # Organization file tools persist synchronously.
        from core.space_sync.personal_metadata import domain

        folders = domain(owner).folders(self.db)
        for path, folder in folders.items():
            if folder and folder.folder_id == project.linked_folder_id:
                return [path + "/" + SOURCE_PATH]
        raise HTTPException(409, "MCP source folder is unavailable")

    def editor(self, app_id, owner, chat_id=""):
        from core.services.application_publication import publication_lock

        with publication_lock(self.engine, app_id):
            project, app = self.ensure(app_id, owner)
            self.bind_chat(project, owner, chat_id)
            source = self.source(project, owner)
            return {
                "app_id": app_id,
                "project_id": project.project_id,
                "project_name": project.name,
                "source_path": SOURCE_PATH,
                "definition": self.decode_draft(source["content"]),
                "content": source["content"],
                "published_definition": json.loads(self.encode(app)),
                "revision": source["revision"],
                "tables": app["tables"],
                "chat_id": self.edit_chat(project, owner),
            }

    def ensure(self, app_id, owner):
        """Caller holds the application publication lock."""
        with self.engine.connect() as connection:
            app = owned_application(connection, app_id, owner)
            binding = (
                connection.execute(
                    select(application_sources).where(application_sources.c.app_id == app_id)
                )
                .mappings()
                .first()
            )
        project_id = binding["project_id"] if binding else "amcp_" + app_id
        self.db.expire_all()
        project = self.db.get(Project, project_id)
        if binding or project:
            if not project or project.deleted_at is not None:
                raise HTTPException(409, "MCP source project has been deleted")
            if resolve_project_permission(self.db, owner, project) not in ("edit", "admin"):
                raise HTTPException(403, "MCP source project is not editable")
            if (project.extra_data or {}).get("hosted_app_id") != app_id:
                raise HTTPException(409, "MCP source project identity conflict")
        else:
            project = self._provision(app, owner, project_id)
        if not binding:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(application_sources).values(
                        app_id=app_id,
                        project_id=project.project_id,
                    )
                )
        files = ProjectSourceService(self.db)
        try:
            files.read(project.project_id, owner, SOURCE_PATH)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            files.write_files(
                project.project_id,
                owner,
                [(SOURCE_PATH, self.encode(app))],
                revisions={SOURCE_PATH: ""},
            )
        return project, app

    def bind_chat(self, project, owner, chat_id):
        if not chat_id:
            return
        chat = self.db.get(ChatSession, chat_id)
        if chat is None:
            return  # Desktop callbacks may originate in a different local store.
        if chat.user_id != owner or chat.deleted_at is not None:
            raise HTTPException(403, "MCP editing requires your own active conversation")
        if any(
            (chat.extra_data or {}).get(key) for key in ("plan_chat", "batch_chat", "workflow_chat")
        ):
            return
        if chat.project_id not in (None, project.project_id):
            return  # Never move a conversation out of another source project.
        chat.project_id = project.project_id
        chat.extra_data = {**(chat.extra_data or {}), "site_chat": True}
        self.db.commit()

    def edit_chat(self, project, owner):
        chat = (
            self.db.query(ChatSession)
            .filter_by(
                project_id=project.project_id,
                user_id=owner,
                deleted_at=None,
                archived=False,
            )
            .filter(ChatSession.extra_data["site_chat"].as_boolean().is_(True))
            .order_by(ChatSession.updated_at.desc())
            .first()
        )
        return chat.chat_id if chat else None

    def _provision(self, app, owner, project_id):
        # These are system-provisioned projects, with stable identity and metadata.
        # Folder creation still uses the ordinary owner lock, validation and audit.
        from core.db.repository import AuditLogRepository
        from core.services.user_folder_service import UserFolderService

        folder_name = "MCP-" + app["id"]
        folder = (
            self.db.query(UserFolder)
            .filter_by(
                user_id=owner,
                parent_folder_id=None,
                name=folder_name,
                deleted_at=None,
            )
            .first()
        )
        if folder is None:
            result = UserFolderService(self.db).create_folder(owner, None, folder_name, owner)
            if not result.ok:
                raise HTTPException(409, result.message)
            folder = self.db.get(UserFolder, result.folder_id)
        if (
            self.db.query(Project)
            .filter(
                Project.linked_folder_id == folder.folder_id,
                Project.deleted_at.is_(None),
            )
            .first()
        ):
            raise HTTPException(409, "MCP source folder is already bound to another project")
        now = utc_now()
        project = Project(
            project_id=project_id,
            name=f"{app['title'][:100]} · {app['id'][:8]}",
            description="Hosted MCP source project",
            kind="personal",
            owner_user_id=owner,
            linked_folder_id=folder.folder_id,
            extra_data={"hosted_app_id": app["id"]},
            instructions=(
                f"This project edits hosted MCP application {app['id']}. "
                "Read mcp.json and manage_application(action='source', app_id='"
                + app["id"]
                + "') before editing. Edit mcp.json tools, then publish with "
                "manage_application(action='publish_project', app_id='"
                + app["id"]
                + "'). Preserve app_id and version. Publication advances version automatically. "
                "This service supports database query tools, not arbitrary server code."
            ),
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
        self.db.add(project)
        self.db.commit()
        AuditLogRepository(self.db).create(
            {
                "user_id": owner,
                "action": "project.created",
                "resource_type": "project",
                "resource_id": project_id,
                "details": {"kind": "personal", "hosted_app_id": app["id"]},
                "status": "success",
            }
        )
        return project

    @staticmethod
    def decode_draft(content):
        try:
            return json.loads(content)
        except ValueError:
            return None  # The editor must remain accessible to repair invalid JSON.

    @staticmethod
    def encode(app):
        return (
            json.dumps(
                {"app_id": app["id"], "version": app["mcp_version"], "tools": app["tools"]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode()

    def source(self, project, owner):
        return ProjectSourceService(self.db).read(project.project_id, owner, SOURCE_PATH)

    def sync(self, project, owner, app_id, revision):
        with self.engine.connect() as connection:
            app = owned_application(connection, app_id, owner)
        return ProjectSourceService(self.db).write_files(
            project.project_id,
            owner,
            [(SOURCE_PATH, self.encode(app))],
            revisions={SOURCE_PATH: revision},
        )
