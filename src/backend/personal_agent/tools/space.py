from core.services.artifact_edition import is_personal_artifact, personal_artifact_predicates
"""Explicit file connector; the personal space is never a sandbox mount."""
import posixpath
from sqlalchemy import select
from core.db.engine import SessionLocal
from core.db.models import Artifact
from core.storage import get_storage
from personal_agent.tasks.guards import owned_task
from personal_agent.tools.responses import response
from personal_agent.workspace.paths import workspace_path
from personal_agent.computer.sandbox import get_personal_sandbox_provider

def enabled(ident, uid):
    state = owned_task(ident, uid)
    if not state["config"].get("space_enabled"): raise PermissionError("Space connector is disabled")
    return state

def register_space_tools(toolkit, ident, uid):
    with SessionLocal() as db:
        from personal_agent.models import PersonalWorkItem, PersonalAgent
        task = db.get(PersonalWorkItem, ident)
        config = db.get(PersonalAgent, task.agent_id).config or {}
    if not config.get("space_enabled"): return

    async def pa_space_search(query: str = ""):
        """Search the owner's personal-space files. Returns IDs, not the entire space."""
        enabled(ident, uid)
        with SessionLocal() as db:
            rows = db.scalars(select(Artifact).where(Artifact.user_id == uid, Artifact.deleted_at.is_(None), *personal_artifact_predicates(Artifact), Artifact.filename.ilike("%" + query + "%")).limit(50)).all()
            return response([{"id": a.artifact_id, "filename": a.filename, "size": a.size_bytes} for a in rows])

    async def pa_space_import(artifact_id: str, destination: str):
        """Import only one authorized file into the task workspace."""
        state = enabled(ident, uid)
        with SessionLocal() as db:
            item = db.get(Artifact, artifact_id)
            if not item or item.user_id != uid or not is_personal_artifact(item) or item.deleted_at:
                raise PermissionError("Space file not available")
            if item.size_bytes > 20*1024*1024: raise ValueError("File exceeds 20 MiB")
            content = get_storage().download_bytes(item.storage_key)
        await get_personal_sandbox_provider().put_file(state["box"], workspace_path(destination), content, user_id=uid)
        return response({"source_id": artifact_id, "destination": destination, "imported": True})

    async def pa_space_save(path: str, filename: str):
        """Save a new output to personal space; duplicate names are rejected by platform policy."""
        state = enabled(ident, uid)
        content = await get_personal_sandbox_provider().get_file(state["box"], workspace_path(path), user_id=uid)
        if len(content) > 20*1024*1024: raise ValueError("File exceeds 20 MiB")
        from core.services.artifact_service import store_bytes_as_artifact
        with SessionLocal() as db:
            item = store_bytes_as_artifact(db, user_id=uid, content=content, filename=posixpath.basename(filename), chat_id=state["chat"], source="ai_generated")
            return response({"artifact_id": item.artifact_id, "filename": item.filename})
    for fn in (pa_space_search, pa_space_import, pa_space_save): toolkit.register_tool_function(fn)
