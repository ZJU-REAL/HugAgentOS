"""Installing from the skill / MCP marketplace is its own permission, separate from self-adding."""

import pytest
import sqlalchemy as sa
from core.auth.capabilities import resolve_capabilities
from core.db.data_repair import copy_permission_bit
from core.db.models import UserShadow
from core.infra.exceptions import AccessDeniedError


def test_market_bits_default_off_and_independent():
    caps = resolve_capabilities({})
    assert caps["can_install_market_skill"] is False and caps["can_install_market_mcp"] is False
    only_market = resolve_capabilities({}, {"__authoritative__": True, "can_install_market_skill": True})
    assert only_market["can_install_market_skill"] is True and only_market["can_add_skill"] is False
    only_own = resolve_capabilities({}, {"can_add_mcp": True})
    assert only_own["can_add_mcp"] is True and only_own["can_install_market_mcp"] is False


def _users(db_session, **metas):
    for uid, meta in metas.items():
        db_session.add(UserShadow(user_id=uid, username=uid, extra_data=meta))
    db_session.commit()


def test_skill_market_install_and_submit_use_their_own_bits(db_session):
    from features.marketplace.api.marketplace import (
        _require_can_add_skill,
        _require_can_install_market_skill,
    )

    _users(db_session, market={"can_install_market_skill": True}, own={"can_add_skill": True})
    _require_can_install_market_skill("market", db_session)
    with pytest.raises(AccessDeniedError):
        _require_can_install_market_skill("own", db_session)
    _require_can_add_skill("own", db_session)
    with pytest.raises(AccessDeniedError):
        _require_can_add_skill("market", db_session)


def test_mcp_market_install_uses_its_own_bit(db_session):
    from features.marketplace.api.mcp_marketplace import (
        _require_can_add_mcp,
        _require_can_install_market_mcp,
    )

    _users(db_session, m_market={"can_install_market_mcp": True}, m_own={"can_add_mcp": True})
    _require_can_install_market_mcp("m_market", db_session)
    with pytest.raises(AccessDeniedError):
        _require_can_install_market_mcp("m_own", db_session)
    with pytest.raises(AccessDeniedError):
        _require_can_add_mcp("m_market", db_session)


def test_market_only_user_can_remove_what_they_installed(db_session):
    from api.routes.v1.me_capabilities import _require_own_or_market

    _users(db_session, r_market={"can_install_market_skill": True}, r_none={})
    _require_own_or_market("r_market", db_session, "skill")
    with pytest.raises(AccessDeniedError):
        _require_own_or_market("r_market", db_session, "mcp")
    with pytest.raises(AccessDeniedError):
        _require_own_or_market("r_none", db_session, "skill")


def test_upgrade_copies_self_add_bits_to_market_bits(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'perm.db'}")
    meta = sa.MetaData()
    users = sa.Table("users_shadow", meta, sa.Column("user_id", sa.String, primary_key=True), sa.Column("metadata", sa.JSON))
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(users.insert(), [
            {"user_id": "off", "metadata": {"can_add_skill": False}},
            {"user_id": "on", "metadata": {"can_add_skill": True, "can_add_mcp": True}},
        ])
        assert copy_permission_bit(conn, "can_add_skill", "can_install_market_skill") == 2
        assert copy_permission_bit(conn, "can_add_mcp", "can_install_market_mcp") == 1
        by_id = dict(conn.execute(sa.select(users.c.user_id, users.c.metadata)).fetchall())
        assert by_id["off"]["can_install_market_skill"] is False
        assert by_id["on"]["can_install_market_skill"] is True and by_id["on"]["can_install_market_mcp"] is True


def test_private_chat_mode_market_installs_follow_permission_and_visibility(db_session, monkeypatch):
    """A private mode installs into its owner's account, so it needs the market bit and visibility."""
    from core.chat import modes
    from features.marketplace import edition as ml
    import core.harness.registry.marketplace_catalog as catalog

    _users(db_session, cm_market={"can_install_market_skill": True}, cm_none={})
    installed = []

    class Catalog:
        def install_marketplace_skill(self, db, slug, owner_user_id=None):
            installed.append((slug, owner_user_id))
            return {"skill_id": f"{owner_user_id}-{slug}"}

    monkeypatch.setattr(catalog, "marketplace_catalog", lambda: Catalog())
    monkeypatch.setattr(ml, "get_hidden_item_ids", lambda db, kind, uid: {"hidden"}, raising=False)
    svc = modes.ChatModeService(db_session)

    out = {"skill_ids": ["market:open", "market:hidden", "own-skill"]}
    svc._install_market_refs(out, "cm_market")
    assert out["skill_ids"] == ["cm_market-open", "own-skill"]

    out = {"skill_ids": ["market:open"]}
    svc._install_market_refs(out, "cm_none")
    assert out["skill_ids"] == []
    # Official modes (no owner) are administrator-configured and keep full access.
    out = {"skill_ids": ["market:hidden"]}
    svc._install_market_refs(out, None)
    assert out["skill_ids"] == ["None-hidden"]
    assert installed == [("open", "cm_market"), ("hidden", None)]


def test_sqlite_copy_runs_once_and_respects_later_follow(tmp_path):
    """SQLite skips alembic; the copy runs once so a later "follow" is not re-copied on restart."""
    from core.db.data_repair import copy_market_permissions_once

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'local.db'}")
    meta = sa.MetaData()
    users = sa.Table("users_shadow", meta, sa.Column("user_id", sa.String, primary_key=True), sa.Column("metadata", sa.JSON))
    sa.Table("content_blocks", meta, sa.Column("id", sa.String, primary_key=True), sa.Column("payload", sa.JSON),
             sa.Column("updated_at", sa.DateTime(timezone=True)), sa.Column("updated_by", sa.String))
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(users.insert(), [{"user_id": "u", "metadata": {"can_add_skill": True}}])
        assert copy_market_permissions_once(conn) is True
        assert conn.execute(sa.select(users.c.metadata)).scalar_one()["can_install_market_skill"] is True
        # The admin sets the market bit back to "follow" (key removed); a restart must not re-copy it.
        conn.execute(users.update().values(metadata={"can_add_skill": True}))
        assert copy_market_permissions_once(conn) is False
        assert "can_install_market_skill" not in conn.execute(sa.select(users.c.metadata)).scalar_one()


def test_plugin_and_agent_market_bits_default_off():
    caps = resolve_capabilities({})
    assert caps["can_install_market_plugin"] is False and caps["can_install_market_agent"] is False


def test_plugin_market_install_and_zip_import_use_their_own_bits(db_session):
    from api.routes.v1.plugins import (
        _require_can_import_plugin,
        _require_can_install_market_plugin,
        _require_plugin_manage,
    )

    _users(db_session, p_market={"can_install_market_plugin": True}, p_own={"can_import_plugin": True}, p_none={})
    _require_can_install_market_plugin("p_market", db_session)
    with pytest.raises(AccessDeniedError):
        _require_can_install_market_plugin("p_own", db_session)
    _require_can_import_plugin("p_own", db_session)
    with pytest.raises(AccessDeniedError):
        _require_can_import_plugin("p_market", db_session)
    # Accepting an upgrade of a plugin you already have: either bit is enough.
    _require_plugin_manage("p_market", db_session)
    _require_plugin_manage("p_own", db_session)
    with pytest.raises(AccessDeniedError):
        _require_plugin_manage("p_none", db_session)


def test_agent_market_install_and_submit_use_their_own_bits(db_session):
    from features.marketplace.api.agent_marketplace import (
        _require_can_add_agent,
        _require_can_install_market_agent,
    )

    _users(db_session, a_market={"can_install_market_agent": True}, a_own={"can_add_agent": True})
    _require_can_install_market_agent("a_market", db_session)
    with pytest.raises(AccessDeniedError):
        _require_can_install_market_agent("a_own", db_session)
    _require_can_add_agent("a_own", db_session)
    with pytest.raises(AccessDeniedError):
        _require_can_add_agent("a_market", db_session)


def test_sqlite_second_copy_runs_on_a_database_that_ran_the_first(tmp_path):
    """A local database that already split skill / MCP still gets the plugin / agent copy once."""
    from datetime import datetime, timezone
    from core.db.data_repair import copy_market_permissions_once

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'local2.db'}")
    meta = sa.MetaData()
    users = sa.Table("users_shadow", meta, sa.Column("user_id", sa.String, primary_key=True), sa.Column("metadata", sa.JSON))
    blocks = sa.Table("content_blocks", meta, sa.Column("id", sa.String, primary_key=True), sa.Column("payload", sa.JSON),
                      sa.Column("updated_at", sa.DateTime(timezone=True)), sa.Column("updated_by", sa.String))
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(blocks.insert().values(id="data-repair:mktperm01", payload={"done": True},
                                            updated_at=datetime.now(timezone.utc), updated_by="t"))
        conn.execute(users.insert(), [{"user_id": "u", "metadata": {
            "can_add_skill": True, "can_import_plugin": True, "can_add_agent": False}}])
        assert copy_market_permissions_once(conn) is True
        meta_row = conn.execute(sa.select(users.c.metadata)).scalar_one()
        # mktperm01 had already run, so the skill bit is left alone.
        assert "can_install_market_skill" not in meta_row
        assert meta_row["can_install_market_plugin"] is True and meta_row["can_install_market_agent"] is False
        assert copy_market_permissions_once(conn) is False


def test_private_chat_mode_plugin_and_agent_installs_follow_their_market_bits(db_session, monkeypatch):
    from core.chat import modes
    from features.marketplace import edition as ml

    _users(db_session, cmp_plugin={"can_install_market_plugin": True, "can_import_plugin": False},
           cmp_own={"can_import_plugin": True, "can_add_agent": True})
    monkeypatch.setattr(ml, "get_hidden_item_ids", lambda db, kind, uid: set(), raising=False)
    svc = modes.ChatModeService(db_session)
    gate = svc._market_gate("cmp_plugin")
    assert gate("plugin", "x") is True and gate("agent", "x") is False
    gate = svc._market_gate("cmp_own")
    assert gate("plugin", "x") is False and gate("agent", "x") is False


def test_market_agent_install_only_brings_bound_items_the_user_may_install(db_session, monkeypatch):
    """Installing a market agent must not install bound market plugins / skills past their own bits."""
    from features.marketplace import edition as ml
    from features.marketplace.install_permission import market_install_gate

    _users(db_session, b_agent={"can_install_market_agent": True},
           b_all={"can_install_market_agent": True, "can_install_market_plugin": True, "can_install_market_skill": True})
    reads = []
    monkeypatch.setattr(ml, "get_hidden_item_ids", lambda db, kind, uid: reads.append(kind) or {"hidden"}, raising=False)
    gate = market_install_gate(db_session, "b_agent")
    assert gate("plugin", "p") is False and gate("skill", "s") is False
    gate = market_install_gate(db_session, "b_all")
    assert gate("plugin", "p") is True and gate("skill", "s") is True
    assert gate("plugin", "hidden") is False
    assert len(reads) == 2  # one hidden-set read per kind, however many items are checked
    # Admin installs (no owner) are not limited.
    assert market_install_gate(db_session, None)("plugin", "hidden") is True


def test_without_build_permission_agent_edits_are_on_off_and_name_only(db_session):
    from core.capabilities.agents.base import edit_needs_build_permission

    _users(db_session, e_market={"can_install_market_agent": True}, e_build={"can_add_agent": True})
    assert edit_needs_build_permission(db_session, "e_market", {"is_enabled": False}) is False
    assert edit_needs_build_permission(db_session, "e_market", {"name": "新名字", "is_enabled": True}) is False
    assert edit_needs_build_permission(db_session, "e_market", {"system_prompt": "x"}) is True
    assert edit_needs_build_permission(db_session, "e_market", {"name": "n", "skill_ids": []}) is True
    assert edit_needs_build_permission(db_session, "e_build", {"system_prompt": "x", "skill_ids": []}) is False
