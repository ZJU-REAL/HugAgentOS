"""CE upgrades retain tool media across workers and repeated reconciliation."""
import base64
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker


def test_tool_media_survives_database_upgrade_and_worker_cache_reset(tmp_path, monkeypatch):
    from core.db.models import ToolMediaBlob
    from core.db.edition_tables import ce_reconcile_schema
    from core.llm import tool_media_store as media

    engine = create_engine(f"sqlite:///{tmp_path / 'media.db'}")
    ce_reconcile_schema(engine)
    assert "tool_media_blobs" in inspect(engine).get_table_names()
    monkeypatch.setattr(media, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(media, "_cache", __import__("collections").OrderedDict())
    monkeypatch.setattr(media, "_cache_bytes", 0)
    encoded = base64.b64encode(b"durable CE image bytes").decode()
    block = {"type": "data", "source": {"type": "base64", "media_type": "image/png", "data": encoded}}
    ref = media.store(block)
    assert ref is not None
    assert media.store(block) == ref
    media._cache.clear()
    media._cache_bytes = 0
    ce_reconcile_schema(engine)
    rows = [{"content": [{"type": "tool_result", "output": [ref]}]}]
    assert media.hydrate_rows(rows)[0]["content"][0]["output"][0]["source"]["data"] == encoded
    with sessionmaker(bind=engine)() as session:
        assert session.query(ToolMediaBlob).count() == 1
    engine.dispose()
