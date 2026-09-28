"""Agent-facing site discovery — one record shape for cloud and local-mode sites.

``publish_site`` resolves its own target; this module only reports what already
exists so the agent can pass an explicit ``site_id`` instead of guessing. Paths
are returned the way the agent must type them: ``/myspace/<folder>`` for cloud
personal projects, the bound host directory for local projects.
"""

from __future__ import annotations

from typing import Any, List

DEFAULT_LIMIT = 10
MAX_LIMIT = 50


def list_sites(user_id: str, chat_id: str = "", limit: int = DEFAULT_LIMIT) -> List[dict]:
    from core.config.local_mode import local_mode_enabled

    if not user_id:
        return []
    try:
        limit = int(limit or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))
    entries = _local_entries(user_id) if local_mode_enabled() else _cloud_entries(user_id)
    current = _chat_project_id(user_id, chat_id)
    for entry in entries:
        entry["in_current_project"] = bool(current) and entry.get("project_id") == current
    entries.sort(key=lambda e: not e["in_current_project"])
    return entries[:limit]


def _chat_project_id(user_id: str, chat_id: str) -> str:
    if not chat_id:
        return ""
    from core.db.engine import SessionLocal
    from core.db.models import ChatSession

    with SessionLocal() as db:
        chat = db.get(ChatSession, chat_id)
        if chat is None or chat.user_id != user_id or chat.deleted_at is not None:
            return ""
        return chat.project_id or ""


def _local_entries(user_id: str) -> List[dict]:
    from core.services.local_site_sources import current_cloud, list_sources
    from fastapi import HTTPException

    cloud, subject = current_cloud()
    if not cloud or not subject:
        raise HTTPException(409, "请先登录云端账号，再查询站点")
    saved_entries = list_sources(user_id)
    if current_cloud() != (cloud, subject):
        raise HTTPException(409, "查询期间云端账号已变化，请重新查询站点")
    entries = []
    for saved in saved_entries:
        source_dir = saved.get("source_dir") or ""
        publish_dir = saved.get("publish_dir") or source_dir
        entries.append(
            {
                "site_id": saved.get("site_id") or "",
                "title": saved.get("title") or "",
                "url": saved.get("url") or "",
                "version": saved.get("version"),
                "kind": "build" if publish_dir != source_dir else "static",
                "source_dir": source_dir,
                "publish_dir": publish_dir,
                "project_id": saved.get("project_id") or "",
                "project_name": saved.get("project_name") or "",
                "editable": bool(source_dir),
            }
        )
    return entries


def _cloud_entries(user_id: str) -> List[dict]:
    from core.db.engine import SessionLocal
    from core.services.site_access_policy import site_management_permission
    from core.services.site_service import SiteService

    with SessionLocal() as db:
        sites, _ = SiteService(db).list_sites(user_id, page=1, page_size=MAX_LIMIT)
        entries = []
        for site in sites:
            if site_management_permission(db, site, user_id) not in ("edit", "admin"):
                continue
            source_dir = _cloud_source_dir(db, site.project_id)
            meta: Any = site.extra_data or {}
            build = meta.get("build") or {}
            entries.append(
                {
                    "site_id": site.site_id,
                    "title": site.title,
                    "url": f"/site/{site.slug}/",
                    "version": site.current_version,
                    "kind": "build" if build else "static",
                    "source_dir": source_dir,
                    "publish_dir": "" if build else source_dir,
                    "project_id": site.project_id or "",
                    "project_name": _project_name(db, site.project_id),
                    "editable": bool(source_dir),
                    "updated_at": site.updated_at.isoformat() if site.updated_at else None,
                }
            )
        return entries


def _live_project(db, project_id: Any):
    from core.db.models import Project

    if not project_id:
        return None
    project = db.get(Project, project_id)
    return None if project is None or project.deleted_at is not None else project


def _project_name(db, project_id: Any) -> str:
    project = _live_project(db, project_id)
    return str(project.name) if project else ""


def _cloud_source_dir(db, project_id: Any) -> str:
    """Where the agent edits this site's source, as a path it can pass to its tools."""
    from core.db.models import UserFolder

    project = _live_project(db, project_id)
    if project is None:
        return ""
    from core.services.edition_workspace import source_directory
    directory = source_directory(project)
    if directory:
        return directory
    if project.kind != "personal" or not project.linked_folder_id:
        return ""
    row = db.query(UserFolder.name).filter(UserFolder.folder_id == project.linked_folder_id).first()
    return f"/myspace/{row[0]}" if row and row[0] else ""
