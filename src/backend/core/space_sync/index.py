"""Durable projection identities outside mounts, for dirty detection and offline renames."""

import json
import sqlite3
from contextlib import contextmanager


@contextmanager
def connection(owner):
    from core.sandbox._common import myspace_cache_dir

    root = myspace_cache_dir(owner).parent.parent / "space_sync"
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "identities.sqlite", timeout=10)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS identities (owner TEXT NOT NULL,path TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(owner,path))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS reflections (owner TEXT NOT NULL,session TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(owner,session))"
        )
        yield db
        db.commit()
    finally:
        db.close()


def get(owner, path):
    with connection(owner) as db:
        row = db.execute(
            "SELECT state FROM identities WHERE owner=? AND path=?", (owner, path)
        ).fetchone()
    return json.loads(row[0]) if row else None


def snapshot(owner):
    with connection(owner) as db:
        rows = db.execute("SELECT path,state FROM identities WHERE owner=?", (owner,)).fetchall()
    return {path: json.loads(state) for path, state in rows}


def remember(owner, path, state):
    with connection(owner) as db:
        db.execute(
            "INSERT INTO identities(owner,path,state) VALUES(?,?,?) ON CONFLICT(owner,path) DO UPDATE SET state=excluded.state",
            (owner, path, json.dumps(state)),
        )


def forget(owner, path, directory=False):
    with connection(owner) as db:
        rows = db.execute("SELECT path FROM identities WHERE owner=?", (owner,)).fetchall()
        db.executemany(
            "DELETE FROM identities WHERE owner=? AND path=?",
            [(owner, p) for (p,) in rows if p == path or directory and p.startswith(path + "/")],
        )


def move(owner, source, destination, directory=False):
    with connection(owner) as db:
        rows = db.execute("SELECT path,state FROM identities WHERE owner=?", (owner,)).fetchall()
        for path, state in rows:
            if path == source or directory and path.startswith(source + "/"):
                db.execute("DELETE FROM identities WHERE owner=? AND path=?", (owner, path))
                db.execute(
                    "INSERT OR REPLACE INTO identities(owner,path,state) VALUES(?,?,?)",
                    (owner, destination + path[len(source) :], state),
                )


def reflection(owner, session, state=None):
    with connection(owner) as db:
        if state is not None:
            db.execute(
                "INSERT OR REPLACE INTO reflections(owner,session,state) VALUES(?,?,?)",
                (owner, session, json.dumps(state)),
            )
            return state
        row = db.execute(
            "SELECT state FROM reflections WHERE owner=? AND session=?", (owner, session)
        ).fetchone()
        return json.loads(row[0]) if row else None
