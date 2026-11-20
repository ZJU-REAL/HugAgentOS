"""Orphan detection, purge and legacy -> current migration for the KB collection."""
from __future__ import annotations

import pytest

from core.infra.time import utc_now
from features.knowledge import vector_client, vector_maintenance
from tests.features.knowledge.kb_vector_support import CURRENT, LEGACY, kb_document


@pytest.fixture
def factory(memory_sessions):
    from features.knowledge.models import KBAsset, KBChunk, KBSpace

    with memory_sessions() as db:
        db.add_all([
            KBSpace(kb_id="kb_live", user_id="u", name="live"),
            KBSpace(kb_id="kb_gone", user_id="u", name="gone", deleted_at=utc_now()),
            kb_document("doc_live"),
            kb_document("doc_deleted", deleted=True),
            kb_document("doc_busy", status="processing"),
            kb_document("doc_in_gone_space", kb_id="kb_gone"),
            KBChunk(chunk_id="p_live", kb_id="kb_live", document_id="doc_live", chunk_index=0, content="c"),
            KBAsset(asset_id="asset_live", kb_id="kb_live", document_id="doc_live",
                    kind="image", storage_key="k", mime_type="image/png"),
        ])
        db.commit()
    return memory_sessions


def row(chunk_id, *, kb="kb_live", doc="doc_live", parent="p_live", row_type="chunk"):
    return {"chunk_id": chunk_id, "parent_chunk_id": parent, "row_type": row_type,
            "kb_id": kb, "document_id": doc, "dense_embedding": [1.0], "user_id": "u",
            "content": "union expenses", "tags_text": "[tag]", "sparse_embedding": {1: 1.0}}


ROWS = [
    row("p_live_0"),
    row("q_p_live_0", row_type="question"),
    row("a_asset_live", row_type="image", parent="a_asset_live"),
    row("x_space", kb="kb_gone", doc="doc_in_gone_space"),
    row("x_nokb", kb="kb_never", doc="doc_live"),
    row("x_doc", doc="doc_deleted"),
    row("x_nodoc", doc="doc_never"),
    row("x_stale", parent="p_old"),
    row("busy_0", doc="doc_busy", parent="p_not_written_yet"),
]
EXPECTED = {
    "x_space": "space_deleted_or_missing",
    "x_nokb": "space_deleted_or_missing",
    "x_doc": "document_deleted_or_missing",
    "x_nodoc": "document_deleted_or_missing",
    "x_stale": "stale_chunk",
}


def test_classifier_flags_only_rows_postgres_no_longer_backs(factory):
    orphans = vector_maintenance.OrphanClassifier(factory).classify(ROWS)

    assert orphans == EXPECTED


def test_stale_image_row_is_flagged(factory):
    orphans = vector_maintenance.OrphanClassifier(factory).classify(
        [row("a_asset_old", row_type="image", parent="a_asset_old")]
    )

    assert orphans == {"a_asset_old": "stale_asset"}


class FakeIterator:
    def __init__(self, rows, batch_size):
        self.pages = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]

    def next(self):
        return self.pages.pop(0) if self.pages else []

    def close(self):
        pass


class FakeMilvus:
    def __init__(self, collections: dict[str, list[dict]], dim=1):
        self.data = {k: list(v) for k, v in collections.items()}
        self.dim = dim
        self.renamed = None
        self.deleted_ids: list[str] = []
        self.lose_rows = 0

    def has_collection(self, name, **_):
        return name in self.data

    def describe_collection(self, name, **_):
        return {"fields": [{"name": "dense_embedding", "params": {"dim": self.dim}}]}

    def query(self, collection_name, output_fields, **_):
        assert output_fields == ["count(*)"]
        return [{"count(*)": len(self.data[collection_name])}]

    def query_iterator(self, collection_name, batch_size, **_):
        return FakeIterator(list(self.data[collection_name]), batch_size)

    def delete(self, collection_name, ids):
        self.deleted_ids.extend(ids)
        self.data[collection_name] = [r for r in self.data[collection_name] if r["chunk_id"] not in ids]

    def insert(self, collection_name, data):
        self.data[collection_name].extend(data[self.lose_rows:])

    def flush(self, _name):
        pass

    def drop_collection(self, name):
        del self.data[name]

    def rename_collection(self, old_name, new_name):
        self.data[new_name] = self.data.pop(old_name)
        self.renamed = (old_name, new_name)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(vector_client, "is_lite", lambda: False)
    monkeypatch.setattr(
        vector_client, "create_collection",
        lambda client, name, dim, **_: client.data.setdefault(name, []),
    )
    monkeypatch.setattr(vector_maintenance, "SCAN_BATCH", 3)
    monkeypatch.setattr(vector_client, "invalidate_collection_cache", lambda: None)


def test_purge_dry_run_deletes_nothing(factory, server):
    client = FakeMilvus({CURRENT: ROWS})

    report = vector_maintenance.purge_orphans(
        client, CURRENT, vector_maintenance.OrphanClassifier(factory), apply=False
    )

    assert report["orphan_rows"] == len(EXPECTED)
    assert report["scanned_rows"] == len(ROWS)
    assert client.deleted_ids == []


def test_purge_apply_deletes_exactly_the_orphans(factory, server):
    client = FakeMilvus({CURRENT: ROWS})

    vector_maintenance.purge_orphans(
        client, CURRENT, vector_maintenance.OrphanClassifier(factory), apply=True
    )

    assert set(client.deleted_ids) == set(EXPECTED)


def test_migrate_copies_live_rows_then_renames(factory, server):
    client = FakeMilvus({LEGACY: ROWS})

    report = vector_maintenance.migrate(
        client, vector_maintenance.OrphanClassifier(factory),
        apply=True, drop_legacy=False, keep_orphans=False,
    )

    assert client.renamed == (CURRENT + vector_maintenance.STAGING_SUFFIX, CURRENT)
    assert {r["chunk_id"] for r in client.data[CURRENT]} == {r["chunk_id"] for r in ROWS} - set(EXPECTED)
    assert LEGACY in client.data
    assert report["copied"] == len(ROWS) - len(EXPECTED)
    copied = client.data[CURRENT][0]
    assert "sparse_embedding" not in copied
    assert copied["sparse_text"] == "union expenses [tag]"


def test_migrate_dry_run_touches_nothing(factory, server):
    client = FakeMilvus({LEGACY: ROWS})

    report = vector_maintenance.migrate(
        client, vector_maintenance.OrphanClassifier(factory),
        apply=False, drop_legacy=True, keep_orphans=False,
    )

    assert set(client.data) == {LEGACY}
    assert report["rows_to_copy"] == len(ROWS) - len(EXPECTED)


def test_migrate_aborts_without_rename_when_counts_differ(factory, server):
    client = FakeMilvus({LEGACY: ROWS})
    client.lose_rows = 1

    with pytest.raises(SystemExit, match="untouched"):
        vector_maintenance.migrate(
            client, vector_maintenance.OrphanClassifier(factory),
            apply=True, drop_legacy=True, keep_orphans=False,
        )

    assert client.renamed is None
    assert CURRENT not in client.data
    assert LEGACY in client.data


def test_migrate_refuses_when_already_migrated(factory, server):
    client = FakeMilvus({LEGACY: ROWS, CURRENT: []})

    with pytest.raises(SystemExit, match="already migrated"):
        vector_maintenance.migrate(
            client, vector_maintenance.OrphanClassifier(factory),
            apply=True, drop_legacy=False, keep_orphans=False,
        )
