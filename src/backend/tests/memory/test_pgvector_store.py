"""Configuration and fail-closed schema checks for external PostgreSQL memory."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from core.memory.pgvector_store import prepare_collection, receipt_rows, vector_config


def test_vector_config_uses_single_pg_database_without_driver_suffix():
    config = vector_config("postgresql+psycopg://owner:p%40ss@db/app?sslmode=require", 2048)
    assert config["provider"] == "pgvector"
    assert config["config"]["connection_string"] == "postgresql://owner:p%40ss@db/app?connect_timeout=10&sslmode=require"
    assert config["config"]["embedding_model_dims"] == 2048
    assert not config["config"]["hnsw"]


@pytest.mark.parametrize("url,dim", [("sqlite:///:memory:", 16), ("postgresql://db/app", 0), ("postgresql://db/app", 16001)])
def test_vector_config_rejects_invalid_backend_and_dimensions(url, dim):
    with pytest.raises(ValueError):
        vector_config(url, dim)


class Cursor:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchone(self):
        return next(self.rows)

    def fetchall(self):
        return list(self.rows)


def store_for(cursor):
    @contextmanager
    def get_cursor(commit=False):
        yield cursor
    return SimpleNamespace(collection_name="hugagent_memories", embedding_model_dims=16, _get_cursor=get_cursor)


def test_existing_dimension_mismatch_keeps_existing_data():
    cursor = Cursor([(1,), ("vector(32)",)])
    store = store_for(cursor)
    with pytest.raises(RuntimeError, match="existing data retained"):
        prepare_collection(store)
    assert not hasattr(store, "_collection_ensured")
    assert all("DROP" not in sql and "DELETE" not in sql and "ALTER" not in sql for sql, _ in cursor.calls)


def test_missing_extension_is_reported_without_trying_install():
    cursor = Cursor([None])
    with pytest.raises(RuntimeError, match="extension installed"):
        prepare_collection(store_for(cursor))
    assert all("CREATE EXTENSION" not in sql for sql, _ in cursor.calls)


def test_receipt_query_has_exact_scope_and_bound_parameters():
    cursor = Cursor([("entry", {"data": "body"})])
    rows = receipt_rows(store_for(cursor), "owner", "workspace", "effect'quoted")
    assert rows == [{"id": "entry", "metadata": {"data": "body"}}]
    sql, params = cursor.calls[0]
    assert "outbox_effect_ids" in sql and "workspace_id" in sql
    assert params == ["owner", "effect'quoted", "effect'quoted", "workspace"]
    assert "effect'quoted" not in sql


def test_new_collection_gets_per_user_and_expiry_indexes():
    cursor = Cursor([(1,), ("vector(16)",)])
    store = store_for(cursor)
    prepare_collection(store)
    statements = " ".join(sql for sql, _ in cursor.calls)
    assert "((payload->>'user_id'))" in statements
    assert "WHERE payload->>'layer' = 'L2'" in statements
    assert store._collection_ensured
