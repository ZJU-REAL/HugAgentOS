"""Operator maintenance for the private-KB Milvus collection.

Run inside the backend container (``PYTHONPATH`` already points at ``src/backend``)::

    python -m features.knowledge.vector_maintenance stats
    python -m features.knowledge.vector_maintenance purge-orphans            # dry run
    python -m features.knowledge.vector_maintenance purge-orphans --apply
    python -m features.knowledge.vector_maintenance migrate                  # dry run
    python -m features.knowledge.vector_maintenance migrate --apply

``purge-orphans`` removes rows nothing in PostgreSQL points at any more: rows of
deleted / missing spaces and documents, chunk and question rows whose parent chunk
is gone (left behind by a re-index whose cleanup failed), and image rows whose
asset is gone. Documents still indexing are skipped for the chunk/asset checks.

``migrate`` copies the legacy IVF_FLAT collection into the current HNSW +
``kb_id`` partition-key collection, leaving orphans behind. Keyword vectors are
rebuilt there by Milvus's BM25 function with a jieba analyzer, which fixes
keyword matching for Chinese (the legacy hashed vectors kept a whole run of
Chinese characters as one token). It builds a staging
collection and renames it only after the row counts match, so the backend never
sees a half-filled collection. Writes made during the copy would be lost: stop
the backend and index workers first and start them again afterwards (a running
process keeps using the collection it resolved at startup). The legacy
collection is kept for rollback unless ``--drop-legacy`` is given.

Every command is a dry run unless ``--apply`` is passed.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any, Callable, Iterator

from features.knowledge import kb_assets_state, kb_vector_state, vector_client
from features.knowledge.vector_indexing import batched

SCAN_FIELDS = ["chunk_id", "parent_chunk_id", "row_type", "kb_id", "document_id"]
COPY_FIELDS = [
    "chunk_id",
    "parent_chunk_id",
    "row_type",
    "user_id",
    "kb_id",
    "document_id",
    "title",
    "content",
    "tags_text",
    "chunk_index",
    "dense_embedding",
]
SCAN_BATCH = 1000
DELETE_BATCH = 500
STAGING_SUFFIX = "_migrating"


def row_count(client, name: str) -> int:
    rows = client.query(
        collection_name=name, filter="", output_fields=["count(*)"], consistency_level="Strong"
    )
    return int(rows[0]["count(*)"]) if rows else 0


def iter_rows(client, name: str, fields: list[str], batch_size: int = SCAN_BATCH) -> Iterator[list[dict]]:
    """Page through a whole collection in primary-key order."""
    it = client.query_iterator(
        collection_name=name, filter="", output_fields=fields, batch_size=batch_size
    )
    try:
        while True:
            page = it.next()
            if not page:
                return
            yield [dict(r) for r in page]
    finally:
        it.close()


def _asset_id(row: dict) -> str | None:
    """The KBAsset id behind an image row (``a_<asset_id>``), else None."""
    cid = str(row.get("chunk_id") or "")
    if row.get("row_type") == kb_assets_state.ROW_TYPE_IMAGE and cid.startswith(kb_assets_state.ASSET_ROW_PREFIX):
        return cid[len(kb_assets_state.ASSET_ROW_PREFIX):]
    return None


class OrphanClassifier:
    """Decide, a page of Milvus rows at a time, which rows PostgreSQL no longer backs."""

    def __init__(self, session_factory: Callable[[], Any]):
        self._session_factory = session_factory

    def classify(self, rows: list[dict]) -> dict[str, str]:
        """Map chunk_id -> reason for every orphan row in ``rows``."""
        from features.knowledge.models import KBAsset, KBChunk, KBDocument, KBSpace

        kb_ids = {r.get("kb_id") for r in rows if r.get("kb_id")}
        doc_ids = {r.get("document_id") for r in rows if r.get("document_id")}
        parent_ids = {
            r.get("parent_chunk_id")
            for r in rows
            if r.get("row_type") in ("chunk", "question") and r.get("parent_chunk_id")
        }
        asset_ids = {a for a in map(_asset_id, rows) if a}
        with self._session_factory() as db:
            live_kbs = {
                k for (k,) in db.query(KBSpace.kb_id).filter(
                    KBSpace.kb_id.in_(kb_ids), KBSpace.deleted_at.is_(None)
                )
            } if kb_ids else set()
            docs = {
                d: (deleted_at, status)
                for d, deleted_at, status in db.query(
                    KBDocument.document_id, KBDocument.deleted_at, KBDocument.indexing_status
                ).filter(KBDocument.document_id.in_(doc_ids))
            } if doc_ids else {}
            live_parents = {
                c for (c,) in db.query(KBChunk.chunk_id).filter(KBChunk.chunk_id.in_(parent_ids))
            } if parent_ids else set()
            live_assets = {
                a for (a,) in db.query(KBAsset.asset_id).filter(KBAsset.asset_id.in_(asset_ids))
            } if asset_ids else set()

        orphans: dict[str, str] = {}
        for r in rows:
            cid = r.get("chunk_id")
            doc = docs.get(r.get("document_id"))
            if r.get("kb_id") not in live_kbs:
                orphans[cid] = "space_deleted_or_missing"
            elif doc is None or doc[0] is not None:
                orphans[cid] = "document_deleted_or_missing"
            elif doc[1] == "processing":
                continue  # an index job may be writing this document right now
            elif r.get("row_type") in ("chunk", "question") and r.get("parent_chunk_id") not in live_parents:
                orphans[cid] = "stale_chunk"
            elif r.get("row_type") == kb_assets_state.ROW_TYPE_IMAGE and _asset_id(r) not in live_assets:
                orphans[cid] = "stale_asset"
        return orphans


def to_current_row(row: dict) -> dict:
    """A legacy row in the current schema: the hashed sparse vector is dropped and
    the text it was built from (chunk text + tags) goes to Milvus's BM25 function,
    which re-derives the keyword vector with Chinese word segmentation. Legacy rows
    only kept the 4096-byte ``content``, so that is the text BM25 gets."""
    text = " ".join(part for part in (row.get("content") or "", row.get("tags_text") or "") if part)
    return vector_client.adapt_rows([{**row, "sparse_text": text}], index="hnsw")[0]


def orphan_pages(client, name: str, classifier: OrphanClassifier, fields: list[str] = SCAN_FIELDS):
    """Yield (page, {chunk_id: reason}) over the whole collection, one page at a time."""
    for page in iter_rows(client, name, fields):
        yield page, classifier.classify(page)


def purge_orphans(client, name: str, classifier: OrphanClassifier, *, apply: bool) -> dict:
    """Delete each page's orphans as they are found; only counters are kept."""
    scanned, reasons = 0, Counter()
    for page, orphans in orphan_pages(client, name, classifier):
        scanned += len(page)
        reasons.update(orphans.values())
        if apply:
            for batch in batched(list(orphans), DELETE_BATCH):
                client.delete(collection_name=name, ids=batch)
    total = sum(reasons.values())
    return {
        "collection": name,
        "scanned_rows": scanned,
        "orphan_rows": total,
        "by_reason": dict(reasons),
        "deleted": total if apply else 0,
        "dry_run": not apply,
    }


def migrate(client, classifier: OrphanClassifier, *, apply: bool, drop_legacy: bool, keep_orphans: bool) -> dict:
    legacy = kb_vector_state.LEGACY_COLLECTION_NAME
    current = kb_vector_state.CURRENT_COLLECTION_NAME
    staging = current + STAGING_SUFFIX
    if vector_client.is_lite():
        raise SystemExit("Milvus Lite keeps the legacy dense-only collection; nothing to migrate.")
    if client.has_collection(current):
        raise SystemExit(f"{current} already exists; the KB is already migrated.")
    if not client.has_collection(legacy):
        raise SystemExit(f"{legacy} does not exist; nothing to migrate.")

    dim = vector_client.collection_dense_dim(client, name=legacy)
    if not dim:
        raise SystemExit(f"Cannot read the dense_embedding dimension of {legacy}.")
    legacy_rows = row_count(client, legacy)
    report: dict[str, Any] = {"from": legacy, "to": current, "dim": dim, "legacy_rows": legacy_rows, "dry_run": not apply}
    reasons: Counter = Counter()
    if not apply:
        if not keep_orphans:
            for _, orphans in orphan_pages(client, legacy, classifier):
                reasons.update(orphans.values())
        skipped = sum(reasons.values())
        report.update(orphans_skipped=skipped, orphans_by_reason=dict(reasons), rows_to_copy=legacy_rows - skipped)
        return report

    if client.has_collection(staging):
        client.drop_collection(staging)  # left by an earlier interrupted run; only ever ours
    vector_client.create_collection(client, staging, dim, lite=False)
    copied = 0
    # One pass: classify and copy each page together.
    for page in iter_rows(client, legacy, COPY_FIELDS):
        orphans = {} if keep_orphans else classifier.classify(page)
        reasons.update(orphans.values())
        keep = [to_current_row(r) for r in page if r.get("chunk_id") not in orphans]
        if keep:
            client.insert(collection_name=staging, data=keep)
            copied += len(keep)
    report.update(orphans_skipped=sum(reasons.values()), orphans_by_reason=dict(reasons))
    client.flush(staging)
    staged = row_count(client, staging)
    report.update(copied=copied, staged_rows=staged)
    if staged != copied:
        raise SystemExit(
            f"Copied {copied} rows but {staging} holds {staged}; left it for inspection, "
            f"{legacy} is untouched and still in use."
        )
    client.rename_collection(old_name=staging, new_name=current)
    report["renamed"] = f"{staging} -> {current}"
    if drop_legacy:
        client.drop_collection(legacy)
        report["legacy_dropped"] = True
    vector_client.invalidate_collection_cache()
    return report


def stats(client) -> dict:
    out: dict[str, Any] = {"lite": vector_client.is_lite()}
    for name in (kb_vector_state.LEGACY_COLLECTION_NAME, kb_vector_state.CURRENT_COLLECTION_NAME):
        exists = bool(client.has_collection(name))
        out[name] = {"exists": exists, "rows": row_count(client, name) if exists else 0}
    vector_client.invalidate_collection_cache()
    out["active"] = vector_client.resolve_collection(client)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m features.knowledge.vector_maintenance",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stats", help="Row counts of the legacy and current collections")
    p_purge = sub.add_parser("purge-orphans", help="Delete rows PostgreSQL no longer backs")
    p_purge.add_argument("--apply", action="store_true", help="Actually delete (default: dry run)")
    p_mig = sub.add_parser("migrate", help="Copy legacy collection into the HNSW + partition-key one")
    p_mig.add_argument("--apply", action="store_true", help="Actually migrate (default: dry run)")
    p_mig.add_argument("--drop-legacy", action="store_true", help="Drop the legacy collection afterwards")
    p_mig.add_argument("--keep-orphans", action="store_true", help="Copy orphan rows too")
    args = parser.parse_args(argv)

    from core.db.engine import SessionLocal

    client = vector_client.get_client()
    classifier = OrphanClassifier(SessionLocal)
    if args.command == "stats":
        result = stats(client)
    elif args.command == "purge-orphans":
        vector_client.invalidate_collection_cache()
        if not vector_client.existing_collection(client):
            result = {"collection": kb_vector_state.COLLECTION_NAME, "exists": False}
        else:
            result = purge_orphans(client, kb_vector_state.COLLECTION_NAME, classifier, apply=args.apply)
    else:
        result = migrate(
            client,
            classifier,
            apply=args.apply,
            drop_legacy=args.drop_legacy,
            keep_orphans=args.keep_orphans,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
