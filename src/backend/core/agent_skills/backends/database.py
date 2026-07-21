"""Database backend for admin-managed skills stored in PostgreSQL."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

from sqlalchemy import func, or_

from .protocol import SkillFileInfo

# Sentinel path used when skill content comes from DB (never actually read)
_DB_SENTINEL = Path("/db/admin_skills")


def _mcp_server_ids_from_header(header: str) -> list[str]:
    """Parse the scalar MCP binding from a bounded SKILL.md frontmatter prefix."""
    match = re.search(r"(?m)^(?:mcp_servers|mcp-server-ids):\s*(.+?)\s*$", header or "")
    if not match:
        return []
    return list(
        dict.fromkeys(
            item.strip() for item in match.group(1).replace(",", " ").split() if item.strip()
        )
    )


class DatabaseBackend:
    """Loads admin skills from PostgreSQL instead of the filesystem.

    Session lifecycle: each method opens and closes its own session to avoid
    holding connections across the global loader's lifetime (which is not
    compatible with FastAPI's dependency-injected sessions).
    """

    def __init__(self, priority: int = 75):
        self._priority = priority

    @property
    def source_name(self) -> str:
        return "admin"

    @property
    def priority(self) -> int:
        return self._priority

    def change_token(self) -> Tuple[int, str]:
        """Return a cheap token representing enabled DB skill changes."""
        SessionLocal, AdminSkill = self._get_session_and_model()
        db = SessionLocal()
        try:
            count, max_updated = (
                db.query(func.count(AdminSkill.skill_id), func.max(AdminSkill.updated_at))
                .filter(AdminSkill.is_enabled == True)
                .one()
            )
            if hasattr(max_updated, "isoformat"):
                max_updated_value = max_updated.isoformat()
            elif max_updated is None:
                max_updated_value = ""
            else:
                max_updated_value = str(max_updated)
            return int(count or 0), max_updated_value
        finally:
            db.close()

    def _get_session_and_model(self):
        """Lazily import DB session and model to avoid startup-time DB connection."""
        from core.db.engine import SessionLocal
        from core.db.models import AdminSkill

        return SessionLocal, AdminSkill

    def list_skill_files(self) -> List[SkillFileInfo]:
        """List enabled DB skills without hydrating instructions or extra files.

        Includes user-private skills (owner_user_id non-null) — the loader is a global
        singleton and must be able to resolve / materialize / register all skills by id.
        Owner isolation is done on the user side (catalog only injects one's own private
        items) and the request side (agent_factory's _filter_skill_ids_for_user drops
        unauthorized ids), not here; otherwise private skills wouldn't exist in _skill_map
        at all → skipped at registration, calls fail.
        """
        SessionLocal, AdminSkill = self._get_session_and_model()
        db = SessionLocal()
        try:
            # Deterministic ordering: without ORDER BY, Postgres row order can drift, and
            # if downstream assembles the prompt in that order it busts the LLM prefix cache.
            # Every runtime-selection field is denormalized except the legacy
            # mcp_servers frontmatter binding. Full SKILL.md and extra_files stay
            # in the database until one of the user's selected skills is materialized.
            rows = (
                db.query(
                    AdminSkill.skill_id,
                    AdminSkill.display_name,
                    AdminSkill.description,
                    AdminSkill.version,
                    AdminSkill.tags,
                    AdminSkill.allowed_tools,
                )
                .filter(AdminSkill.is_enabled == True)
                .order_by(AdminSkill.skill_id)
                .all()
            )
            # Old rows persist MCP ids only inside SKILL.md. Fetch full content
            # for that small subset instead of hydrating every enabled skill.
            mcp_bindings = {
                row.skill_id: _mcp_server_ids_from_header(row.skill_content)
                for row in (
                    db.query(AdminSkill.skill_id, AdminSkill.skill_content)
                    .filter(
                        AdminSkill.is_enabled == True,
                        or_(
                            AdminSkill.skill_content.contains("mcp_servers:"),
                            AdminSkill.skill_content.contains("mcp-server-ids:"),
                        ),
                    )
                    .all()
                )
            }
            result = []
            for row in rows:
                result.append(
                    SkillFileInfo(
                        skill_id=row.skill_id,
                        file_path=_DB_SENTINEL / row.skill_id / "SKILL.md",
                        source_name=self.source_name,
                        priority=self._priority,
                        metadata={
                            "id": row.skill_id,
                            "name": row.display_name or row.skill_id,
                            "description": row.description or "",
                            "version": row.version or "1.0.0",
                            "tags": list(row.tags or []),
                            "allowed_tools": list(row.allowed_tools or []),
                            "mcp_server_ids": mcp_bindings.get(row.skill_id, []),
                        },
                        is_database=True,
                    )
                )
            return result
        finally:
            db.close()

    def read_skill_file(self, skill_id: str) -> str:
        """Read raw SKILL.md content from DB."""
        SessionLocal, AdminSkill = self._get_session_and_model()
        db = SessionLocal()
        try:
            content = (
                db.query(AdminSkill.skill_content).filter(AdminSkill.skill_id == skill_id).scalar()
            )
            if content is None:
                raise FileNotFoundError(f"Admin skill not found in DB: {skill_id}")
            return str(content)
        finally:
            db.close()

    def get_extra_files(self, skill_id: str) -> dict:
        """Return {filename: content} or empty dict for a skill's extra files."""
        SessionLocal, AdminSkill = self._get_session_and_model()
        db = SessionLocal()
        try:
            extra_files = (
                db.query(AdminSkill.extra_files).filter(AdminSkill.skill_id == skill_id).scalar()
            )
            if not extra_files:
                return {}
            return dict(extra_files)
        finally:
            db.close()

    def exists(self, skill_id: str) -> bool:
        """Check if an admin skill exists in the database."""
        SessionLocal, AdminSkill = self._get_session_and_model()
        db = SessionLocal()
        try:
            count = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).count()
            return count > 0
        finally:
            db.close()
