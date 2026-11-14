"""Sites have their own permission bit, independent of the lab permission."""

import pytest
import sqlalchemy as sa
from core.auth.capabilities import resolve_capabilities
from core.db.data_repair import copy_lab_permission_to_sites
from core.db.models import UserShadow
from core.infra.exceptions import AccessDeniedError
from features.sites.api import sites as sites_api
from features.sites.permission import require_sites_permission
from features.workbench.catalog import permitted


def test_sites_default_on_and_independent_of_lab():
    assert resolve_capabilities({})["sites_enabled"] is True
    lab_off = resolve_capabilities({"lab_enabled": False})
    assert lab_off["lab_enabled"] is False and lab_off["sites_enabled"] is True
    sites_off = resolve_capabilities({"sites_enabled": False})
    assert sites_off["lab_enabled"] is True and sites_off["sites_enabled"] is False


def test_sites_follow_role_and_team_layers():
    assert resolve_capabilities({}, {}, {"sites_enabled": False})["sites_enabled"] is False
    assert resolve_capabilities({"sites_enabled": True}, {}, {"sites_enabled": False})["sites_enabled"] is True
    # An authoritative role layer grants only what it lists.
    granted = resolve_capabilities({}, {"__authoritative__": True, "sites_enabled": True})
    assert granted["sites_enabled"] is True and granted["lab_enabled"] is False
    assert resolve_capabilities({}, {"__authoritative__": True, "lab_enabled": True})["sites_enabled"] is False


def _builtin(identifier):
    return {"applicationKey": "builtin:" + identifier, "declaration": {"id": identifier}}


def test_catalog_filters_lab_and_sites_separately():
    layout = {"hidden": []}
    caps = {"lab_enabled": False, "sites_enabled": True, "allowed_apps": None}
    assert permitted(_builtin("sites"), caps, layout) is True
    assert permitted(_builtin("lab"), caps, layout) is False
    caps = {"lab_enabled": True, "sites_enabled": False, "allowed_apps": None}
    assert permitted(_builtin("sites"), caps, layout) is False
    assert permitted(_builtin("lab"), caps, layout) is True


def test_sites_api_requires_the_sites_permission(db_session):
    assert any(dep.dependency is require_sites_permission for dep in sites_api.router.dependencies)
    db_session.add_all([
        UserShadow(user_id="sites_on", username="sites_on", extra_data={"lab_enabled": False}),
        UserShadow(user_id="sites_off", username="sites_off", extra_data={"sites_enabled": False}),
    ])
    db_session.commit()

    class User:
        def __init__(self, user_id):
            self.user_id = user_id

    require_sites_permission(User("sites_on"), db_session)
    with pytest.raises(AccessDeniedError):
        require_sites_permission(User("sites_off"), db_session)


def test_upgrade_skips_missing_organization_tables(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ce.db'}")
    meta = sa.MetaData()
    users = sa.Table("users_shadow", meta, sa.Column("user_id", sa.String, primary_key=True), sa.Column("metadata", sa.JSON))
    meta.create_all(engine)
    with engine.begin() as conn:
        assert copy_lab_permission_to_sites(conn) == 0

        conn.execute(users.insert(), {"user_id": "disabled", "metadata": {"lab_enabled": False}})
        assert copy_lab_permission_to_sites(conn) == 1
        assert conn.execute(sa.select(users.c.metadata)).scalar_one() == {"lab_enabled": False, "sites_enabled": False}
        assert copy_lab_permission_to_sites(conn) == 0
