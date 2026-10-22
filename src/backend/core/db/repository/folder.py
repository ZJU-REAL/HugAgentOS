"""Transaction-scoped owner locks shared by folder and artifact writers."""
import hashlib
from threading import Lock
from sqlalchemy import event
from sqlalchemy.orm import Session

_SQLITE_LOCKS = [Lock() for _ in range(64)]


def _release_sqlite_locks(session, transaction):
    if transaction.parent is None:
        for lock in session.info.pop("folder_upload_locks", []):
            lock.release()


def lock_folder_owner(db: Session, owner_model, owner_column: str, owner_id: str) -> None:
    if db.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE. Its supported single-process deployment still
        # serves requests on multiple threads, so retain a bounded process lock
        # until commit/rollback/close, including early-return and error paths.
        if not db.in_transaction():
            db.begin()
        if not db.info.get("folder_upload_lock_listener"):
            event.listen(db, "after_transaction_end", _release_sqlite_locks)
            db.info["folder_upload_lock_listener"] = True
        digest = hashlib.sha256((owner_model.__tablename__ + ":" + owner_id).encode()).digest()
        lock = _SQLITE_LOCKS[int.from_bytes(digest[:2], "big") % len(_SQLITE_LOCKS)]
        held = db.info.setdefault("folder_upload_locks", [])
        if lock not in held:
            lock.acquire()
            held.append(lock)
    # NO KEY UPDATE does not conflict with FK KEY SHARE taken by artifact inserts.
    owner = (
        db.query(owner_model)
        .filter(getattr(owner_model, owner_column) == owner_id)
        .with_for_update(key_share=True)
        .first()
    )
    if owner is None:
        raise ValueError("文件夹所属空间不存在")
