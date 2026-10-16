"""Private, per-account storage for this computer's Personal Agent link.

Each cloud account gets its own directory under the local data root, so switching
desktop accounts never lets one account's commands run under another's link.
"""

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from core.config.runtime_env import local_data_dir


@dataclass
class Link:
    device_id: str
    token: str
    name: str
    cloud_base: str
    control_enabled: bool
    sessions_enabled: bool


def account_dir(account: str) -> Path:
    return local_data_dir() / "device-link" / hashlib.sha256(account.encode()).hexdigest()[:32]


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def load(account: str) -> Optional[Link]:
    try:
        return Link(**json.loads((account_dir(account) / "link.json").read_text()))
    except FileNotFoundError:
        return None


def save(account: str, link: Link) -> None:
    folder = _private_dir(account_dir(account))
    temporary = folder / "link.json.tmp"
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(asdict(link), stream)
    os.replace(temporary, folder / "link.json")


def delete(account: str) -> None:
    folder = account_dir(account)
    for name in ("link.json", "receipts.sqlite"):
        (folder / name).unlink(missing_ok=True)


class Journal:
    """Durable receipts. A started action is never executed again, even after a crash."""

    def __init__(self, account: str):
        path = _private_dir(account_dir(account)) / "receipts.sqlite"
        self.db = sqlite3.connect(path)
        if os.name != "nt":
            os.chmod(path, 0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS receipts "
            "(kind TEXT NOT NULL, id TEXT NOT NULL, state TEXT NOT NULL, result TEXT, PRIMARY KEY (kind, id))"
        )
        # The process cannot know whether an action it started before stopping took effect.
        self.db.execute(
            "UPDATE receipts SET state='done', result=? WHERE state='started'",
            (json.dumps({"outcome_unknown": True, "error": "interrupted; check the result before retry"}),),
        )
        self.db.commit()

    def seen(self, kind: str, ident: str) -> bool:
        return self.db.execute("SELECT 1 FROM receipts WHERE kind=? AND id=?", (kind, ident)).fetchone() is not None

    def start(self, kind: str, ident: str) -> None:
        self.db.execute("INSERT INTO receipts VALUES (?, ?, 'started', NULL)", (kind, ident))
        self.db.commit()

    def finish(self, kind: str, ident: str, result) -> None:
        self.db.execute(
            "INSERT INTO receipts VALUES (?, ?, 'done', ?) "
            "ON CONFLICT(kind, id) DO UPDATE SET state='done', result=excluded.result",
            (kind, ident, json.dumps(result)),
        )
        self.db.commit()

    def pending(self):
        return [
            (kind, ident, json.loads(result))
            for kind, ident, result in self.db.execute(
                "SELECT kind, id, result FROM receipts WHERE state='done'"
            ).fetchall()
        ]

    def close_receipt(self, kind: str, ident: str) -> None:
        self.db.execute(
            "UPDATE receipts SET state='closed', result=NULL WHERE kind=? AND id=?", (kind, ident)
        )
        self.db.execute(
            "DELETE FROM receipts WHERE state='closed' AND rowid NOT IN "
            "(SELECT rowid FROM receipts ORDER BY rowid DESC LIMIT 1000)"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
