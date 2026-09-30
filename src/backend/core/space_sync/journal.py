"""Durable coalescing queue outside the sandbox mount; acknowledgements use generation CAS."""

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .events import Change, rebase_move


class Journal:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, generation INTEGER NOT NULL, payload TEXT NOT NULL, ready REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS event_sequence (id INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
        if not self._db.execute(
            "SELECT 1 FROM sqlite_sequence WHERE name='event_sequence'"
        ).fetchone():
            maximum = self._db.execute("SELECT MAX(generation) FROM events").fetchone()[0]
            if maximum:
                self._db.execute("INSERT INTO event_sequence(id) VALUES(?)", (maximum,))
                self._db.execute("DELETE FROM event_sequence")
        self._db.commit()

    def put(self, key, change, delay=0):
        saved = self.put_many([(key, change, delay)])
        return (saved.get(key) or next(iter(saved.values())))[0]

    def put_many(self, entries):
        keys = set()
        with self._lock, self._db:
            stored_parents = self._db.execute(
                "SELECT key,payload FROM events WHERE json_extract(payload,'$.directory')=1 AND json_extract(payload,'$.kind')='moved'"
            ).fetchall()
            parents = [(key, Change(**json.loads(payload))) for key, payload in stored_parents]
            incoming_parents = [
                (key, change)
                for key, change, delay in entries
                if change.directory and change.kind == "moved"
            ]
            parents.extend(incoming_parents)
            entries = list(entries)
            # A child rename may already be durable when its parent moves next.
            # Move its queue key in this transaction, issuing a fresh generation.
            if incoming_parents:
                pending = self._db.execute(
                    "SELECT key,payload,ready FROM events WHERE json_extract(payload,'$.kind')='moved'"
                ).fetchall()
                for old_key, payload, ready in pending:
                    child = Change(**json.loads(payload))
                    rebased = child
                    for parent_key, parent in incoming_parents:
                        if old_key.split(":", 1)[0] == parent_key.split(":", 1)[0]:
                            rebased = rebase_move(rebased, parent)
                    if rebased != child:
                        self._db.execute("DELETE FROM events WHERE key=?", (old_key,))
                        entries.append(
                            (
                                old_key.split(":", 1)[0] + ":" + rebased.path,
                                rebased,
                                max(0, ready - time.time()),
                            )
                        )
            # Parent directory identities must be applied before synthetic child events.
            entries = sorted(
                entries,
                key=lambda entry: (
                    not (entry[1].kind == "moved" and entry[1].directory),
                    entry[1].source.count("/"),
                ),
            )
            for key, change, delay in entries:
                namespace = key.split(":", 1)[0]
                for parent_key, parent in parents:
                    if parent_key != key and parent_key.split(":", 1)[0] == namespace:
                        change = rebase_move(change, parent)
                key = namespace + ":" + change.path
                if change.kind == "moved":
                    old_key = key.split(":", 1)[0] + ":" + change.source
                    previous = self._db.execute(
                        "SELECT payload FROM events WHERE key=?", (old_key,)
                    ).fetchone()
                    if previous:
                        prior = Change(**json.loads(previous[0]))
                        if prior.kind == "moved" and prior.destination == change.source:
                            change = Change(
                                "moved",
                                prior.source,
                                change.destination,
                                change.directory,
                                change.observed,
                            )
                            self._db.execute("DELETE FROM events WHERE key=?", (old_key,))
                            keys.discard(old_key)
                prior = self._db.execute(
                    "SELECT payload FROM events WHERE key=?", (key,)
                ).fetchone()
                if prior and change.kind in ("modified", "closed"):
                    previous = Change(**json.loads(prior[0]))
                    if previous.kind == "moved":
                        change = previous
                generation = self._db.execute("INSERT INTO event_sequence DEFAULT VALUES").lastrowid
                self._db.execute("DELETE FROM event_sequence WHERE id=?", (generation,))
                self._db.execute(
                    "INSERT INTO events(key,generation,payload,ready) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET generation=excluded.generation,payload=excluded.payload,ready=excluded.ready,attempts=0,error=NULL",
                    (key, generation, json.dumps(asdict(change)), time.time() + delay),
                )
                keys.add(key)
            rows = {
                key: self._db.execute(
                    "SELECT generation,payload FROM events WHERE key=?", (key,)
                ).fetchone()
                for key in keys
            }
        return {key: (row[0], Change(**json.loads(row[1]))) for key, row in rows.items() if row}

    def get(self, key):
        with self._lock:
            row = self._db.execute(
                "SELECT generation,payload FROM events WHERE key=?", (key,)
            ).fetchone()
        return (row[0], Change(**json.loads(row[1]))) if row else None

    def pending(self, *, ready=False):
        with self._lock:
            rows = self._db.execute(
                "SELECT key,generation,payload,attempts,ready FROM events ORDER BY ready"
            ).fetchall()
        entries = [
            (key, generation, Change(**json.loads(payload)), attempts, deadline)
            for key, generation, payload, attempts, deadline in rows
        ]
        parents = [
            (key, change)
            for key, g, change, a, deadline in entries
            if change.kind == "moved" and change.directory
        ]
        result = []
        for key, generation, change, attempts, deadline in entries:
            if ready and deadline > time.time():
                continue
            if ready and any(
                parent_key != key
                and parent_key.split(":", 1)[0] == key.split(":", 1)[0]
                and any(change.path.startswith(path + "/") for path in parent.paths())
                for parent_key, parent in parents
            ):
                continue
            result.append((key, generation, change, attempts))
        return sorted(
            result,
            key=lambda row: (
                not (row[2].kind == "moved" and row[2].directory),
                row[2].source.count("/"),
            ),
        )

    def acknowledge(self, key, generation):
        with self._lock, self._db:
            self._db.execute("DELETE FROM events WHERE key=? AND generation=?", (key, generation))

    def retry(self, key, generation, error):
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT attempts FROM events WHERE key=? AND generation=?", (key, generation)
            ).fetchone()
            if row:
                attempts = row[0] + 1
                delay = min(60, 2 ** min(attempts, 6))
                self._db.execute(
                    "UPDATE events SET attempts=?, ready=?, error=? WHERE key=? AND generation=?",
                    (
                        attempts,
                        time.time() + delay,
                        type(error).__name__,
                        key,
                        generation,
                    ),
                )
                return delay
        return 2

    def close(self):
        with self._lock:
            self._db.close()
