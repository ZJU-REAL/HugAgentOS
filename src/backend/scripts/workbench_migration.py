"""Explicit isolated-copy migration command. No automatic startup migration."""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Workbench inventory and isolated-copy rehearsal")
    parser.add_argument("operation", choices=["inventory", "apply", "rollback", "clone-origin"])
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--digest")
    parser.add_argument("--delta", type=Path)
    parser.add_argument("--writes-frozen", action="store_true")
    args = parser.parse_args()
    # A dedicated variable prevents accidental use of deployment DATABASE_URL.
    from sqlalchemy.engine import make_url

    url = make_url(os.environ.get("WB20_COPY_DATABASE_URL", "sqlite://"))
    if url.get_backend_name() == "sqlite":
        database = Path(url.database or "").resolve()
        isolated = Path.cwd() / ".scratch" / "workbench-20"
        if not database.is_relative_to(isolated.resolve()):
            parser.error("SQLite copy must be under this worktree .scratch/workbench-20")
    elif url.get_backend_name() == "postgresql":
        if url.host not in {"localhost", "127.0.0.1", "::1"} or not (url.database or "").startswith(
            "workbench_20"
        ):
            parser.error("PostgreSQL rehearsal requires loopback and workbench_20 database prefix")
    else:
        parser.error("Unsupported copy database")
    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    from core.applications import migration
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(url)
    with Session(engine) as db:
        if args.operation == "inventory":
            result = migration.inventory(db, args.snapshot)
        else:
            if not args.digest or not args.writes_frozen:
                parser.error("Mutation requires reviewed digest and --writes-frozen")
            if args.operation == "apply":
                result = migration.apply(db, args.snapshot, args.digest, writes_frozen=True)
            elif args.operation == "clone-origin":
                result = migration.clone_origin(db, args.snapshot, args.digest, writes_frozen=True)
            else:
                if args.delta is None:
                    parser.error("Rollback requires --delta protected export path")
                result = migration.rollback(
                    db, args.snapshot, args.digest, args.delta, writes_frozen=True
                )
    print(json.dumps(result, sort_keys=True))
    engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        # Domain errors contain codes only; DB URLs and payloads never print.
        raise SystemExit(str(error)) from None
    except Exception as error:
        raise SystemExit("migration_failed:" + type(error).__name__) from None
