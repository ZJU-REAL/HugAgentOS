"""Product-specific PostgreSQL operations around mem0's pgvector adapter.

CRUD and similarity search remain in mem0. These operations enforce schema
identity and query durable effect receipts without a capped result scan.
"""
from __future__ import annotations

from sqlalchemy.engine import make_url

COLLECTION = "hugagent_memories"


def vector_config(database_url: str, dimensions: int) -> dict:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("pgvector memory requires the platform PostgreSQL database")
    if not 1 <= dimensions <= 16000:
        raise ValueError("Memory vector dimension must be between 1 and 16000")
    # psycopg expects a libpq URI, not SQLAlchemy's driver suffix.
    uri = url.set(drivername="postgresql").update_query_dict({"connect_timeout": "10"}).render_as_string(hide_password=False)
    return {"provider": "pgvector", "config": {
        "connection_string": uri, "collection_name": COLLECTION,
        "embedding_model_dims": dimensions, "minconn": 1, "maxconn": 10,
        # Exact cosine search supports model widths beyond ANN index limits.
        "hnsw": False, "diskann": False,
    }}


def is_pgvector(memory) -> bool:
    config = getattr(getattr(memory, "config", None), "vector_store", None)
    return getattr(config, "provider", None) == "pgvector"


def prepare_collection(store) -> None:
    """Create only a table; never install extensions or replace existing vectors."""
    if store.collection_name != COLLECTION:
        raise ValueError("Unexpected memory collection")
    dimensions = int(store.embedding_model_dims)
    if not 1 <= dimensions <= 16000:
        raise ValueError("Invalid memory vector dimension")
    with store._get_cursor(commit=True) as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('hugagent.memory.schema'))")
        cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        if cursor.fetchone() is None:
            raise RuntimeError("The platform database must have the vector extension installed")
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS hugagent_memories "
            f"(id UUID PRIMARY KEY, vector vector({dimensions}), payload JSONB)"
        )
        cursor.execute(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'hugagent_memories'::regclass "
            "AND attname = 'vector' AND NOT attisdropped"
        )
        row = cursor.fetchone()
        if row is None or row[0] != f"vector({dimensions})":
            raise RuntimeError("Memory embedding dimension differs from stored vectors; existing data retained")
        cursor.execute("CREATE INDEX IF NOT EXISTS hugagent_memories_payload_idx "
                       "ON hugagent_memories USING gin(payload)")
        # mem0 and the product queries filter with payload->>'key' = value, which
        # the GIN index cannot serve; without these every search scans all users.
        cursor.execute("CREATE INDEX IF NOT EXISTS hugagent_memories_user_idx "
                       "ON hugagent_memories ((payload->>'user_id'))")
        cursor.execute("CREATE INDEX IF NOT EXISTS hugagent_memories_expiry_idx "
                       "ON hugagent_memories ((payload->>'expiration_date'), id) "
                       "WHERE payload->>'layer' = 'L2'")
    store._collection_ensured = True


def receipt_rows(store, user_id: str, workspace_id: str | None, effect_id: str) -> list[dict]:
    params = [user_id, effect_id, effect_id]
    query = ("SELECT id, payload FROM hugagent_memories WHERE payload->>'user_id' = %s "
             "AND (payload->>'outbox_effect_id' = %s "
             "OR COALESCE(payload->'outbox_effect_ids', '[]'::jsonb) ? %s)")
    if workspace_id:
        query += " AND payload->>'workspace_id' = %s"
        params.append(workspace_id)
    query += " ORDER BY id LIMIT 5"
    with store._get_cursor() as cursor:
        cursor.execute(query, params)
        return [{"id": str(row[0]), "metadata": row[1]} for row in cursor.fetchall()]


def expiry_candidates(store, scan_limit: int, today: str) -> list[dict]:
    if scan_limit < 1:
        return []
    with store._get_cursor() as cursor:
        # Expiry is checked again by the common Python policy. SQL does not cast
        # untrusted metadata to dates; malformed dates must never trigger deletion.
        cursor.execute(
            "SELECT id, payload FROM hugagent_memories WHERE payload->>'layer' = 'L2' "
            "AND (payload->>'expiration_date' IS NULL OR payload->>'expiration_date' <= %s "
            "OR payload->>'expiration_date' !~ '^\\d{4}-\\d{2}-\\d{2}$') "
            "ORDER BY payload->>'expiration_date' NULLS LAST, id LIMIT %s", (today, scan_limit))
        return [{"id": str(row[0]), "metadata": row[1]} for row in cursor.fetchall()]


def close_vector_store(store) -> None:
    pool = getattr(store, "connection_pool", None)
    if pool is not None:
        close = getattr(pool, "close", None) or pool.closeall
        close()
    else:
        client = getattr(store, "client", None)
        if client is not None:
            client.close()
