"""Verified archival shared by cloud migrations and desktop SQLite startup."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

revision = "site05retire"
TABLES = ("site_kv", "site_submissions")


def retire_site_storage(engine, directory: Path):
    with engine.begin() as connection:
        return retire_connection(connection, directory, strict=False)


def _digest(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def retire_connection(connection, directory: Path, *, strict=True):
    existing = [name for name in TABLES if sa.inspect(connection).has_table(name)]
    if connection.dialect.name == "postgresql":
        for name in existing:
            connection.execute(sa.text(f'LOCK TABLE "{name}" IN ACCESS EXCLUSIVE MODE'))
    if not existing:
        return None
    snapshot = {}
    for name in existing:
        table = sa.Table(name, sa.MetaData(), autoload_with=connection)
        snapshot[name] = [dict(row) for row in connection.execute(sa.select(table)).mappings()]
    # Round-trip dates into portable JSON before hashing and verification.
    payload = json.loads(
        json.dumps(snapshot, default=lambda value: value.isoformat(), ensure_ascii=False)
    )
    if any(payload.values()):
        receipts = list(directory.glob("kv-to-sql-*/receipt.json"))
        verified = False
        for candidate in receipts:
            receipt = json.loads(candidate.read_text())
            covered = {site["site_id"] for site in receipt.get("sites", [])}
            required = {row["site_id"] for rows in payload.values() for row in rows}
            if (
                receipt.get("verified") is True
                and receipt.get("legacy_sha256") == _digest(payload)
                and required <= covered
            ):
                verified = True
                break
        if not verified and not strict:
            print("Legacy site data retained: SQL migration verification is pending")
            return None
        if not verified:
            raise RuntimeError(
                "Complete and verify site SQL migration before retiring nonempty legacy storage"
            )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = directory / f"site-storage-{uuid4().hex}.json"
    manifest = {
        "revision": revision,
        "sha256": _digest(payload),
        "tables": payload,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with archive.open("x", encoding="utf-8") as handle:
        os.chmod(archive, 0o600)
        json.dump(manifest, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    verified = json.loads(archive.read_text(encoding="utf-8"))
    if verified["sha256"] != _digest(verified["tables"]) or verified["tables"] != payload:
        raise RuntimeError("Retired storage backup verification failed")
    for name in existing:
        sa.Table(name, sa.MetaData(), autoload_with=connection).drop(connection)
    # No row values, credentials or private fields enter logs.
    print(f"Retired site storage archived: {archive}; rows={sum(map(len, payload.values()))}")

    return archive
