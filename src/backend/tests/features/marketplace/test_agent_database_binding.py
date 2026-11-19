"""Packaged agents bind databases by the portable id ``database_query``."""
import pytest

from core.db.models import AdminMcpServer
from features.marketplace.agent_installation import _expand_portable_bindings


@pytest.fixture(autouse=True)
def owner():
    from features.datasources import extension

    extension.setup()  # the datasources feature declares the alias


def _row(server_id, *, managed=True, enabled=True, owner=None):
    return AdminMcpServer(
        server_id=server_id, display_name=server_id, transport="streamable_http", is_enabled=enabled,
        owner_user_id=owner, extra_config={"managed_by": "datasources"} if managed else {},
    )


def test_portable_binding_expands_to_reachable_connectors(db_session):
    db_session.add_all([_row("db_sales"), _row("db_hr"), _row("db_off", enabled=False),
                        _row("dbu_mine", owner="u-1"), _row("dbu_theirs", owner="u-2"),
                        _row("db_lookalike", managed=False)])
    db_session.flush()
    available = {"db_sales", "db_hr", "db_off", "dbu_mine", "dbu_theirs", "db_lookalike", "web_fetch"}
    out = _expand_portable_bindings(db_session, ["web_fetch", "database_query"], available, "u-1")
    assert out == ["web_fetch", "db_hr", "db_sales", "dbu_mine"]


def test_without_connectors_the_portable_id_is_kept_and_later_dropped(db_session):
    assert _expand_portable_bindings(db_session, ["database_query"], set(), None) == ["database_query"]
    assert _expand_portable_bindings(db_session, ["web_fetch"], {"web_fetch"}, None) == ["web_fetch"]
