"""Durable controller leases; Docker labels are immutable after creation."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class ExpirationStore:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv(
            "OPENSANDBOX_EXPIRATION_DB", "/var/lib/opensandbox/expirations.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS expirations "
                       "(sandbox_id TEXT PRIMARY KEY, expires_at TEXT NOT NULL)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, sandbox_id):
        with self.connection() as db:
            row = db.execute("SELECT expires_at FROM expirations WHERE sandbox_id=?",
                             (sandbox_id,)).fetchone()
        return row[0] if row else None

    def put(self, sandbox_id, expires_at):
        with self.connection() as db:
            db.execute("INSERT INTO expirations VALUES (?, ?) "
                       "ON CONFLICT(sandbox_id) DO UPDATE SET expires_at=excluded.expires_at",
                       (sandbox_id, expires_at))

    def delete(self, sandbox_id):
        with self.connection() as db:
            db.execute("DELETE FROM expirations WHERE sandbox_id=?", (sandbox_id,))
