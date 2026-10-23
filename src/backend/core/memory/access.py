"""Exact L2 mutation authorization, shared by HTTP admission and replay."""
from __future__ import annotations

import asyncio
from typing import Optional

from core.db.threads import run_in_session
from core.memory.context import MemoryContext
from core.memory import backend


def _project_memory_owner(db, project_id: str, actor: str) -> Optional[str]:
    """The memory scope ``actor`` may edit in a project, or None when it may not."""
    from core.auth.edition import resolve_project_access
    from core.db.models import Project
    from core.projects.edition import project_memory_policy

    project = db.query(Project).filter_by(project_id=project_id, deleted_at=None).first()
    if project is None:
        return None
    access = resolve_project_access(db, actor, project)
    if access.level not in ("edit", "admin"):
        return None
    policy = project_memory_policy(db, project.project_id, actor)
    if policy is not None:
        # Read availability does not revoke explicit edits of existing memory.
        return policy[1]
    # CE has no policy row for local projects; actor access is checked above.
    return actor if project.kind == "local" else None


async def authorized_memory_context(memory_id: str, actor: str) -> Optional[MemoryContext]:
    """Resolve trusted actor access from the stored scope, never a client scope."""
    if not memory_id or not actor or not backend.settings.memory.enabled:
        return None
    memory = await asyncio.to_thread(backend._get_memory)
    if memory is None:
        raise RuntimeError("memory store unavailable")
    try:
        item = await asyncio.to_thread(memory.get, memory_id)
    except IndexError:
        # mem0 MilvusDB.get indexes its empty result for an absent ID.
        # Normalize only that missing-row error; transport failures still raise.
        return None
    if not isinstance(item, dict):
        return None
    metadata = item.get("metadata") or {}
    scope = item.get("user_id") or metadata.get("user_id")
    workspace = str(metadata.get("workspace_id") or "default")
    if workspace.startswith("project:"):
        expected = await run_in_session(_project_memory_owner, workspace[len("project:"):], actor)
        if expected is None or scope != expected:
            return None
    elif scope != actor:
        return None
    return MemoryContext(user_id=actor, workspace_id=workspace,
                         scope_user_id=str(scope), actor=actor, write_enabled=True)
