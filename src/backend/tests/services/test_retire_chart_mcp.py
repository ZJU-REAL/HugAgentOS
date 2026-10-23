"""Chart retirement removes configuration without deleting user output or shared models."""

import pytest
import sqlalchemy as sa

from core.db.retire_chart_mcp import retire_chart_mcp


@pytest.fixture
def chart_db():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    specs = {
        "admin_mcp_servers": [("server_id", sa.String, True)],
        "mcp_market_items": [("slug", sa.String, True)],
        "mcp_market_versions": [
            ("version_id", sa.String, True),
            ("slug", sa.String, False),
            ("source_server_id", sa.String, False),
        ],
        "mcp_market_installations": [
            ("install_id", sa.String, True),
            ("slug", sa.String, False),
            ("server_id", sa.String, False),
        ],
        "mcp_market_submissions": [
            ("submission_id", sa.String, True),
            ("slug", sa.String, False),
            ("source_server_id", sa.String, False),
        ],
        "marketplace_listing_states": [
            ("id", sa.String, True),
            ("kind", sa.String, False),
            ("item_id", sa.String, False),
        ],
        "model_role_assignments": [
            ("role_key", sa.String, True),
            ("provider_id", sa.String, False),
        ],
        "model_providers": [("provider_id", sa.String, True)],
        "catalog_overrides": [
            ("override_id", sa.String, True),
            ("kind", sa.String, False),
            ("item_id", sa.String, False),
        ],
        "agent_market_submissions": [
            ("submission_id", sa.String, True),
            ("bindings_snapshot", sa.JSON, False),
        ],
        "admin_skills": [
            ("skill_id", sa.String, True),
            ("allowed_tools", sa.JSON, False),
            ("skill_content", sa.Text, False),
            ("updated_at", sa.DateTime, False),
        ],
        "scheduled_tasks": [("task_id", sa.String, True), ("enabled_mcp_ids", sa.JSON, False)],
        "user_agents": [("agent_id", sa.String, True), ("mcp_server_ids", sa.JSON, False)],
        "content_blocks": [("id", sa.String, True), ("payload", sa.JSON, False)],
        "artifacts": [("artifact_id", sa.String, True), ("name", sa.String, False)],
    }
    tables = {
        name: sa.Table(name, metadata, *[sa.Column(n, t, primary_key=pk) for n, t, pk in fields])
        for name, fields in specs.items()
    }
    sa.ForeignKeyConstraint(
        [tables["mcp_market_versions"].c.slug], [tables["mcp_market_items"].c.slug]
    )
    sa.ForeignKeyConstraint(
        [tables["mcp_market_installations"].c.server_id], [tables["admin_mcp_servers"].c.server_id]
    )
    sa.ForeignKeyConstraint(
        [tables["mcp_market_installations"].c.slug], [tables["mcp_market_items"].c.slug]
    )
    with engine.connect() as bind:
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")
    metadata.create_all(engine)
    with engine.begin() as bind:
        for sid in ("generate_chart_tool", "chart-clone", "internet_search"):
            bind.execute(tables["admin_mcp_servers"].insert().values(server_id=sid))
        bind.execute(tables["mcp_market_items"].insert(), [{"slug": "charts"}, {"slug": "search"}])
        bind.execute(
            tables["mcp_market_versions"]
            .insert()
            .values(version_id="v1", slug="charts", source_server_id="generate_chart_tool")
        )
        bind.execute(
            tables["mcp_market_installations"]
            .insert()
            .values(install_id="i1", slug="charts", server_id="chart-clone")
        )
        bind.execute(
            tables["mcp_market_submissions"]
            .insert()
            .values(submission_id="s1", slug="charts", source_server_id="generate_chart_tool")
        )
        bind.execute(
            tables["marketplace_listing_states"].insert(),
            [
                {"id": "l1", "kind": "mcp", "item_id": "charts"},
                {"id": "l2", "kind": "skill", "item_id": "charts"},
            ],
        )
        bind.execute(tables["model_providers"].insert().values(provider_id="shared"))
        bind.execute(
            tables["model_role_assignments"].insert(),
            [
                {"role_key": "chart", "provider_id": "shared"},
                {"role_key": "main_agent", "provider_id": "shared"},
            ],
        )
        bind.execute(
            tables["user_agents"]
            .insert()
            .values(
                agent_id="a1",
                mcp_server_ids=["generate_chart_tool", "chart-clone", "internet_search"],
            )
        )
        bind.execute(
            tables["content_blocks"].insert(),
            [
                {
                    "id": "catalog_overrides",
                    "payload": {"mcp": [{"id": "generate_chart_tool"}, {"id": "internet_search"}]},
                },
                {
                    "id": "prompt_versions",
                    "payload": {"text": "historical generate_chart_tool guidance"},
                },
            ],
        )
        bind.execute(
            tables["catalog_overrides"].insert(),
            [
                {"override_id": "o1", "kind": "mcp", "item_id": "generate_chart_tool"},
                {"override_id": "o2", "kind": "skill", "item_id": "generate_chart_tool"},
            ],
        )
        bind.execute(
            tables["agent_market_submissions"]
            .insert()
            .values(
                submission_id="a1",
                bindings_snapshot={
                    "mcp_server_ids": ["generate_chart_tool", "internet_search"],
                    "skill_ids": ["skill"],
                },
            )
        )
        bind.execute(
            tables["admin_skills"]
            .insert()
            .values(skill_id="skill", allowed_tools=["generate_chart_tool", "web_fetch"])
        )
        bind.execute(
            tables["scheduled_tasks"]
            .insert()
            .values(task_id="task", enabled_mcp_ids=["chart-clone", "internet_search"])
        )
        bind.execute(
            tables["artifacts"].insert().values(artifact_id="png", name="chart_existing.png")
        )
    yield engine, tables
    engine.dispose()


def test_retirement_removes_market_clones_bindings_and_role(chart_db):
    engine, tables = chart_db
    with engine.begin() as bind:
        retire_chart_mcp(bind)
        assert bind.execute(sa.select(tables["admin_mcp_servers"].c.server_id)).scalars().all() == [
            "internet_search"
        ]
        for name in ("mcp_market_versions", "mcp_market_installations", "mcp_market_submissions"):
            assert bind.execute(sa.select(sa.func.count()).select_from(tables[name])).scalar() == 0
        assert bind.execute(sa.select(tables["mcp_market_items"].c.slug)).scalars().all() == [
            "search"
        ]
        assert bind.execute(
            sa.select(tables["marketplace_listing_states"].c.kind)
        ).scalars().all() == ["skill"]
        assert bind.execute(sa.select(tables["user_agents"].c.mcp_server_ids)).scalar() == [
            "internet_search"
        ]
        assert bind.execute(
            sa.select(tables["model_role_assignments"].c.role_key)
        ).scalars().all() == ["main_agent"]
        assert bind.execute(sa.select(tables["catalog_overrides"].c.kind)).scalars().all() == [
            "skill"
        ]
        assert bind.execute(
            sa.select(tables["agent_market_submissions"].c.bindings_snapshot)
        ).scalar() == {"mcp_server_ids": ["internet_search"], "skill_ids": ["skill"]}
        assert bind.execute(sa.select(tables["admin_skills"].c.allowed_tools)).scalar() == [
            "web_fetch"
        ]
        assert bind.execute(sa.select(tables["scheduled_tasks"].c.enabled_mcp_ids)).scalar() == [
            "internet_search"
        ]
        assert bind.execute(sa.select(tables["model_providers"].c.provider_id)).scalar() == "shared"
        assert bind.execute(sa.select(tables["artifacts"].c.name)).scalar() == "chart_existing.png"
        payload = bind.execute(
            sa.select(tables["content_blocks"].c.payload).where(
                tables["content_blocks"].c.id == "catalog_overrides"
            )
        ).scalar()
        assert payload == {"mcp": [{"id": "internet_search"}]}
        # Prompt edits are a separate, explicitly reviewed content operation.
        assert "generate_chart_tool" in str(
            bind.execute(
                sa.select(tables["content_blocks"].c.payload).where(
                    tables["content_blocks"].c.id == "prompt_versions"
                )
            ).scalar()
        )
        assert retire_chart_mcp(bind) == {}


def test_retirement_rolls_back_atomically(chart_db):
    engine, tables = chart_db
    with engine.connect() as bind:
        transaction = bind.begin()
        retire_chart_mcp(bind)
        transaction.rollback()
        assert (
            bind.execute(
                sa.select(sa.func.count()).select_from(tables["admin_mcp_servers"])
            ).scalar()
            == 3
        )
        assert (
            bind.execute(
                sa.select(tables["model_role_assignments"].c.role_key).where(
                    tables["model_role_assignments"].c.role_key == "chart"
                )
            ).scalar()
            == "chart"
        )


def test_chart_role_cannot_be_listed_assigned_or_imported(db_session):
    from core.models import repository as repository
    from core.db.models import ModelRoleAssignment

    assert "chart" not in {
        role["role_key"] for role in repository.list_role_assignments(db_session)
    }
    assert repository.assign_role(db_session, "chart", "unused") is False
    assert (
        repository.import_all(
            db_session, {"role_assignments": [{"role_key": "chart", "provider_id": "unused"}]}
        )["imported_roles"]
        == 0
    )
    assert db_session.query(ModelRoleAssignment).filter_by(role_key="chart").count() == 0


def test_rejected_submission_slug_reuse_does_not_delete_another_mcp(chart_db):
    engine, tables = chart_db
    with engine.begin() as bind:
        bind.execute(
            tables["mcp_market_submissions"]
            .insert()
            .values(submission_id="old", slug="search", source_server_id="generate_chart_tool")
        )
        bind.execute(
            tables["mcp_market_versions"]
            .insert()
            .values(version_id="search-v1", slug="search", source_server_id="internet_search")
        )
        bind.execute(
            tables["mcp_market_installations"]
            .insert()
            .values(install_id="search-i1", slug="search", server_id="internet_search")
        )
        retire_chart_mcp(bind)
        assert (
            bind.execute(sa.select(tables["mcp_market_installations"].c.server_id)).scalar()
            == "internet_search"
        )
        assert (
            bind.execute(sa.select(tables["mcp_market_versions"].c.version_id)).scalar()
            == "search-v1"
        )


def test_skill_frontmatter_binding_is_removed_without_rewriting_body(chart_db):
    engine, tables = chart_db
    body = "# Instructions\nMention generate_chart_tool in this historical explanation.\n"
    content = (
        "---\nname: analyst\nmcp_servers: generate_chart_tool, internet_search\nallowed_tools: generate_chart_tool web_fetch\n---\n"
        + body
    )
    with engine.begin() as bind:
        from datetime import datetime

        old_updated_at = datetime(2020, 1, 1)
        bind.execute(
            tables["admin_skills"].update().values(skill_content=content, updated_at=old_updated_at)
        )
        retire_chart_mcp(bind)
        result = bind.execute(sa.select(tables["admin_skills"].c.skill_content)).scalar()
        assert result.endswith(body)
        assert (
            bind.execute(sa.select(tables["admin_skills"].c.updated_at)).scalar() > old_updated_at
        )
        header = result.split("---")[1]
        assert "generate_chart_tool" not in header
        assert "internet_search" in header and "web_fetch" in header
