"""Read-only full comparison of live personal files, mirror paths and stored bytes."""
import json
from core.db.engine import SessionLocal
from core.db.models import Artifact
from core.myspace.projection import _folder_rel
from core.myspace.reconciliation import iter_mirror_files
from core.sandbox._common import myspace_cache_dir, myspace_cache_root
from core.storage import get_storage
from core.services.artifact_edition import personal_artifact_predicates


def audit():
    report = dict(users=0, files=0, missing=0, extra=0, byte_mismatch=0, failures=0)
    with SessionLocal() as db:
        users = {r[0] for r in db.query(Artifact.user_id).filter(
            *personal_artifact_predicates(Artifact), Artifact.deleted_at.is_(None)).distinct()}
        users.update(p.name for p in myspace_cache_root().iterdir() if p.is_dir())
        for uid in sorted(users):
            expected = {}
            for art in db.query(Artifact).filter(
                Artifact.user_id == uid, *personal_artifact_predicates(Artifact), Artifact.deleted_at.is_(None)
            ):
                directory = _folder_rel(db, uid, art.user_folder_id, {})
                if directory is None:
                    report["failures"] += 1
                    continue
                rel = (directory + "/" if directory else "") + art.filename
                if rel in expected:
                    report["failures"] += 1
                expected[rel] = art
            actual = {entry.rel for entry in iter_mirror_files(uid)}
            report["missing"] += len(set(expected) - actual)
            report["extra"] += len(actual - set(expected))
            for rel in set(expected) & actual:
                try:
                    if (myspace_cache_dir(uid) / rel).read_bytes() != get_storage().download_bytes(expected[rel].storage_key):
                        report["byte_mismatch"] += 1
                except Exception:
                    report["failures"] += 1
            report["users"] += 1
            report["files"] += len(expected)
    print(json.dumps(report), flush=True)
    return any(report[k] for k in ("missing", "extra", "byte_mismatch", "failures"))


if __name__ == "__main__":
    raise SystemExit(audit())
