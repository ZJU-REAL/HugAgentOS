"""Preview by default; apply only an exact, previously saved plan. No object deletion."""
import argparse
import json
import os
from pathlib import Path
from core.db.engine import SessionLocal
from core.myspace.legacy_paths import plan_repairs, apply_repairs, restore_repairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        if args.apply or args.rollback:
            plan = json.loads(args.plan.read_text())
            if plan["user_id"] != args.user_id:
                parser.error("Plan owner does not match --user-id")
            count = restore_repairs(db, plan) if args.rollback else apply_repairs(db, plan)
            db.commit()
            print(json.dumps({"repaired": count, "objects_deleted": 0}))
        else:
            plan = plan_repairs(db, args.user_id)
            # Never overwrite an existing reviewed plan.
            fd = os.open(args.plan, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(plan, stream, ensure_ascii=False, indent=2)
            print(json.dumps({"planned": len(plan["repairs"]), "skipped": len(plan["skipped"]),
                              "plan": str(args.plan)}))


if __name__ == "__main__":
    main()
