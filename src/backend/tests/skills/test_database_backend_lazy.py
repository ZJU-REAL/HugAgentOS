from __future__ import annotations

from pathlib import Path

from sqlalchemy import event


def test_database_skill_listing_projects_metadata_and_defers_full_content(
    db_session,
    monkeypatch,
):
    from core.agent_skills.backends.database import DatabaseBackend
    from core.db.models import AdminSkill

    full_content = (
        "---\n"
        "name: lazy-skill\n"
        "display_name: Lazy Skill\n"
        "description: Load only when selected\n"
        "mcp_servers: company_mcp\n"
        "---\n\n"
        "## Instructions\n\n" + "large instructions\n" * 1000
    )
    db_session.add(
        AdminSkill(
            skill_id="lazy-skill",
            skill_content=full_content,
            display_name="Lazy Skill",
            description="Load only when selected",
            version="2.0.0",
            tags=["ontology:Enterprise"],
            allowed_tools=["search_company"],
            extra_files={"reference.txt": "large reference"},
            is_enabled=True,
        )
    )
    db_session.commit()

    backend = DatabaseBackend()
    monkeypatch.setattr(
        backend,
        "_get_session_and_model",
        lambda: (lambda: db_session, AdminSkill),
    )
    statements: list[str] = []

    def capture_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", capture_sql)
    try:
        items = backend.list_skill_files()
    finally:
        event.remove(bind, "before_cursor_execute", capture_sql)

    item = next(entry for entry in items if entry.skill_id == "lazy-skill")
    assert item.content is None
    assert item.is_database is True
    assert item.metadata == {
        "id": "lazy-skill",
        "name": "Lazy Skill",
        "description": "Load only when selected",
        "version": "2.0.0",
        "tags": ["ontology:Enterprise"],
        "allowed_tools": ["search_company"],
        "mcp_server_ids": ["company_mcp"],
    }
    select_sql = "\n".join(statements).lower()
    assert "admin_skills.extra_files" not in select_sql
    assert "admin_skills.skill_content as admin_skills_skill_content" in select_sql

    assert backend.read_skill_file("lazy-skill") == full_content


def test_loader_reads_full_database_skill_only_on_demand():
    from core.agent_skills.backends.composite import CompositeBackend
    from core.agent_skills.backends.protocol import SkillFileInfo
    from core.agent_skills.loader import MultiSourceSkillLoader

    full_content = (
        "---\n"
        "name: lazy-skill\n"
        "display_name: Lazy Skill\n"
        "description: Load only when selected\n"
        "---\n\n"
        "## Instructions\n\nRun the selected workflow.\n"
    )

    class Backend:
        source_name = "admin"
        priority = 75

        def __init__(self):
            self.full_reads = 0

        def list_skill_files(self):
            return [
                SkillFileInfo(
                    skill_id="lazy-skill",
                    file_path=Path("/db/admin_skills/lazy-skill/SKILL.md"),
                    source_name=self.source_name,
                    priority=self.priority,
                    metadata={
                        "id": "lazy-skill",
                        "name": "Lazy Skill",
                        "description": "Load only when selected",
                        "version": "1.0.0",
                        "tags": [],
                        "allowed_tools": [],
                        "mcp_server_ids": [],
                    },
                    is_database=True,
                )
            ]

        def read_skill_file(self, skill_id):
            self.full_reads += 1
            return full_content

        def get_extra_files(self, skill_id):
            return {}

        def exists(self, skill_id):
            return skill_id == "lazy-skill"

    backend = Backend()
    loader = MultiSourceSkillLoader(CompositeBackend([backend]))

    assert loader.load_all_metadata()["lazy-skill"].name == "Lazy Skill"
    assert backend.full_reads == 0
    assert loader.load_skill_full("lazy-skill").instructions
    assert backend.full_reads == 1
