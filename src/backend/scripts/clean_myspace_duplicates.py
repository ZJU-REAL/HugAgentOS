#!/usr/bin/env python3
"""Preview or clean ALL duplicate personal path groups; stop writers before --apply."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    from core.db.engine import engine
    from core.myspace.duplicate_cleanup import clean_duplicates, duplicate_rows
    from core.sandbox._common import myspace_cache_dir
    import sqlalchemy as sa

    with engine.connect() as conn:
        rows = duplicate_rows(conn)
    groups = {(r["user_id"], r["user_folder_id"], r["filename"]) for r in rows}
    print(json.dumps({"groups": len(groups), "records": len(rows),
                      "users": len({r["user_id"] for r in rows}), "apply": args.apply}))
    if not args.apply:
        return
    if args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    backup = args.backup_dir.resolve()
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Keep paths for physical cleanup in the backup so interruption can be recovered.
    paths = {}
    def save(journal):
        folders = sa.Table("user_folders", sa.MetaData(), autoload_with=conn)
        mapping = {r["folder_id"]: r for r in conn.execute(sa.select(folders)).mappings()}
        for row in journal["rows"]:
            names, current, seen = [], row["user_folder_id"], set()
            while current is not None:
                if current in seen or current not in mapping:
                    raise RuntimeError("Broken folder chain; no deletion has been committed")
                seen.add(current)
                folder = mapping[current]
                if folder["user_id"] != row["user_id"]:
                    raise RuntimeError("Folder owner mismatch; cleanup aborted")
                names.append(folder["name"])
                current = folder["parent_folder_id"]
            rel = "/".join([*reversed(names), row["filename"]])
            root = myspace_cache_dir(row["user_id"]).resolve()
            source = root / rel
            if not source.resolve().is_relative_to(root):
                raise RuntimeError("Unsafe mirror path; cleanup aborted")
            key = hashlib.sha256((row["user_id"] + "\x00" + rel).encode()).hexdigest()
            entry = {"user_id": row["user_id"], "rel": rel, "backup": key}
            if source.is_file():
                shutil.copy2(source, backup / key)
                entry["sha256"] = hashlib.sha256((backup / key).read_bytes()).hexdigest()
            paths[key] = entry
        journal["mirror_paths"] = list(paths.values())
        payload = json.dumps(journal, ensure_ascii=False, default=lambda v: v.isoformat())
        destination = backup / "journal.json"
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        destination.chmod(0o600)

    with engine.begin() as conn:
        journal = clean_duplicates(conn, before_delete=save)
    for entry in paths.values():
        source = myspace_cache_dir(entry["user_id"]) / entry["rel"]
        source.unlink(missing_ok=True)
    print(json.dumps({"removed_records": len(journal["rows"]),
                      "removed_mirror_paths": len(paths), "backup": str(backup)}))


if __name__ == "__main__":
    main()
