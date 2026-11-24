"""A capability change committed by one worker reaches every worker's caches on the next lookup."""
import pytest
from sqlalchemy.orm import sessionmaker

from core.db.capability_revision import read_revision
from core.db.models import AdminMcpServer
from tests.db_support import share_factory


@pytest.fixture
def factory(db_session, monkeypatch):
    factory = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr("core.capabilities.mcp.service.SessionLocal", factory)
    share_factory(monkeypatch, factory)
    monkeypatch.setattr("core.db.capability_revision._CHECK_INTERVAL_S", 0.0)
    return factory


def _server(server_id, **kw):
    return AdminMcpServer(server_id=server_id, display_name=server_id, transport="streamable_http",
                          url="https://example.org/mcp", is_enabled=True, **kw)


def test_orm_writes_bump_the_revision(factory):
    with factory() as db:
        before = read_revision(db)
        db.add(_server("rev_a"))
        db.commit()
        after_insert = read_revision(db)
        assert after_insert and after_insert != before

        db.get(AdminMcpServer, "rev_a").display_name = "renamed"
        db.commit()
        after_update = read_revision(db)
        assert after_update != after_insert

        db.query(AdminMcpServer).filter(AdminMcpServer.server_id == "rev_a").delete()
        db.commit()
        assert read_revision(db) != after_update


def test_skill_writes_bump_the_revision_too(factory):
    from core.db.models import AdminSkill

    with factory() as db:
        before = read_revision(db)
        db.add(AdminSkill(skill_id="rev-skill", skill_content="---\nname: x\n---\n", display_name="x", description="x"))
        db.commit()
        assert read_revision(db) != before


def test_unrelated_writes_leave_the_revision_alone(factory):
    from core.db.models import ContentBlock

    with factory() as db:
        db.add(_server("rev_b"))
        db.commit()
        revision = read_revision(db)
        db.add(ContentBlock(id="unrelated", payload={}))
        db.commit()
        assert read_revision(db) == revision


def test_second_worker_sees_a_change_without_waiting_for_the_ttl(factory):
    from core.capabilities.mcp.service import McpServerConfigService

    worker_a, worker_b = McpServerConfigService(), McpServerConfigService()
    with factory() as db:
        db.add(_server("rev_c"))
        db.commit()
    assert "rev_c" in worker_b.get_all_servers()

    # Worker A disables it; worker B never had its cache invalidated directly.
    with factory() as db:
        db.get(AdminMcpServer, "rev_c").is_enabled = False
        db.commit()
    worker_a.invalidate_cache()
    assert "rev_c" not in worker_a.get_all_servers()
    assert "rev_c" not in worker_b.get_all_servers()
