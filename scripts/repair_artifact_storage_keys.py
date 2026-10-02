#!/usr/bin/env python3
"""Dry-run by default. Apply or undo verified historical storage-key repairs."""

import argparse
import json

from core.content.artifact_key_repair import repair_legacy_key, rollback_legacy_key, verified_legacy_key
from core.db.engine import SessionLocal
from core.db.models import Artifact
from core.storage import get_storage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", action="store_true")
    parser.add_argument("--user-id", required=True, help="Explicit owner scope; never scans all accounts")
    args = parser.parse_args()
    storage = get_storage()
    with SessionLocal() as db:
        # Page by identity, never hold every artifact or a server-side cursor during commits.
        cursor = ""
        while True:
            rows = db.query(Artifact).filter(
                Artifact.user_id == args.user_id, Artifact.artifact_id > cursor,
            ).order_by(Artifact.artifact_id).limit(200).all()
            if not rows:
                break
            cursor = rows[-1].artifact_id
            for artifact in rows:
                try:
                    if args.rollback:
                        if (artifact.extra_data or {}).get("storage_key_repair"):
                            changed = rollback_legacy_key(db, artifact)
                            print(json.dumps({"id": artifact.artifact_id, "rolled_back": changed}))
                        continue
                    new_key = verified_legacy_key(artifact, storage)
                    if not new_key:
                        continue
                    result = {"id": artifact.artifact_id, "old_key": artifact.storage_key, "new_key": new_key}
                    if args.apply:
                        result["applied"] = repair_legacy_key(db, artifact, storage)
                    print(json.dumps(result, ensure_ascii=False))
                except Exception as error:
                    db.rollback()
                    # Do not print SDK exception strings which may include signed URLs.
                    print(json.dumps({"id": artifact.artifact_id, "error": type(error).__name__}))
                    raise SystemExit(1) from None


if __name__ == "__main__":
    main()
