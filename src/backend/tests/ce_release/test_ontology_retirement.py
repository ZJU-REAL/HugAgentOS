"""CE upgrades delete ontology governance data and keep everything else."""

import json

import sqlalchemy as sa

from core.db.ontology_retirement import ONTOLOGY_TABLES, has_ontology_data, retire_ontology


def _seed(engine):
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE ontology_packs (pack_id TEXT PRIMARY KEY)"))
        connection.execute(sa.text(
            "CREATE TABLE ontology_pack_versions (version_id TEXT PRIMARY KEY, "
            "pack_id TEXT REFERENCES ontology_packs(pack_id))"
        ))
        connection.execute(sa.text("INSERT INTO ontology_packs VALUES ('risk')"))
        connection.execute(sa.text("INSERT INTO ontology_pack_versions VALUES ('v1', 'risk')"))
        connection.execute(sa.text(
            "CREATE TABLE system_configs (config_key TEXT PRIMARY KEY, config_value TEXT)"
        ))
        connection.execute(sa.text(
            "INSERT INTO system_configs VALUES "
            "('ontology.force_plugin_import_build_validation', 'true'), ('memory.enabled', 'true')"
        ))
        connection.execute(sa.text("CREATE TABLE users_shadow (user_id TEXT PRIMARY KEY, metadata TEXT)"))
        connection.execute(sa.text("INSERT INTO users_shadow VALUES (:id, :meta)"), {
            "id": "u1",
            "meta": json.dumps({"ontology_enabled": True, "ontology_pack_ids": ["risk"],
                                "can_use_ontology_validation": True, "language": "en"}),
        })
        connection.execute(sa.text("CREATE TABLE user_agents (agent_id TEXT PRIMARY KEY, extra_config TEXT)"))
        connection.execute(sa.text("INSERT INTO user_agents VALUES (:id, :extra)"), {
            "id": "a1", "extra": json.dumps({"ontology_tags": ["ontology:risk"], "icon": "x"}),
        })


def test_retire_ontology_drops_tables_and_strips_settings(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ce.db'}")
    _seed(engine)

    with engine.begin() as connection:
        assert has_ontology_data(connection)
        retire_ontology(connection)

    with engine.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        assert not tables & set(ONTOLOGY_TABLES)
        assert has_ontology_data(connection) is False
        keys = {row[0] for row in connection.execute(sa.text("SELECT config_key FROM system_configs"))}
        assert keys == {"memory.enabled"}
        meta = json.loads(connection.execute(sa.text("SELECT metadata FROM users_shadow")).scalar_one())
        assert meta == {"language": "en"}
        extra = json.loads(connection.execute(sa.text("SELECT extra_config FROM user_agents")).scalar_one())
        assert extra == {"icon": "x"}


def test_retire_ontology_is_a_no_op_on_a_clean_database(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'clean.db'}")
    with engine.begin() as connection:
        assert has_ontology_data(connection) is False
        assert retire_ontology(connection) == {}
