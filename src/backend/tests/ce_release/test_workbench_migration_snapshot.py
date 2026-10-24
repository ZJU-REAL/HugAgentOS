"""Actual community operator snapshot service: ownership retained, incompatible DB rejected."""
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from features.workbench.migration import inventory
from features.workbench.migration_snapshot import read_snapshot
from core.db.edition import ce_create_all
from core.db.models import ContentBlock, InstalledPlugin, UserShadow, CatalogOverride


def _database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'operator.db'}")
    ce_create_all(engine)
    return engine


def test_ce_snapshot_preserves_private_install_and_legacy_user_data(tmp_path):
    engine = _database(tmp_path)
    try:
        with Session(engine) as db:
            db.add(UserShadow(user_id="owner", user_center_id="owner", username="Owner",
                              extra_data={"permissions": {"allowed_apps": ["chat"]},
                                          "workbenchPreferences": {"private": True}}))
            db.add(InstalledPlugin(install_id="private@owner", slug="private", name="Private",
                                   owner_user_id="owner", source="imported_codex",
                                   ui_contributions=[], component_ids={"skills": ["owned"]}))
            db.add(ContentBlock(id="app_config", payload={"items": []}, updated_by="test"))
            db.flush()
            db.add(CatalogOverride(user_id="owner", kind="skill", item_id="owned",
                                   enabled=False, config_data={"pinned": True}))
            db.commit()
            target = tmp_path / "private-snapshot.json"
            result = inventory(db, target)
            assert result["digest"]
            source = read_snapshot(target)["source"]
            assert source["plugins"]["private@owner"]["owner"] == "owner"
            assert source["users"]["owner"]["workbenchPreferences"] == {"private": True}
            assert source["permissions"]["overrides"] == [
                {"user": "owner", "kind": "skill", "item": "owned", "enabled": False,
                 "config": {"pinned": True}}]
            assert "roles" not in source["permissions"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", ["roles", "teams", "team_members", "role_assignments", "team_folders"])
def test_ce_inventory_refuses_noncommunity_identity_tables_before_snapshot(tmp_path, table):
    engine = _database(tmp_path)
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)'))
            connection.execute(text(f'INSERT INTO "{table}" (id) VALUES (42)'))
        target = tmp_path / "must-not-write.json"
        with Session(engine) as db:
            with pytest.raises(ValueError, match="ce_identity_database_requires_full_edition_operator"):
                inventory(db, target)
            assert db.execute(text(f'SELECT id FROM "{table}"')).scalar() == 42
            assert not target.exists()
    finally:
        engine.dispose()
