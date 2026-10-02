#!/usr/bin/env python3
"""Backfill disposable UI previews using the configured DB. Dry run by default."""
import argparse
from core.db.engine import SessionLocal
from core.db.models import ChatMessage
from core.db.history_projection import backfill_display


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        if not args.apply:
            count = (
                db.query(ChatMessage.message_id)
                .filter(ChatMessage.tool_calls_display.is_(None))
                .count()
            )
            print(f"{count} rows without a display projection; pass --apply to populate previews.")
            return
        count = backfill_display(db)
        db.commit()
        print(f"Populated {count} tool-result display projections; canonical data unchanged.")


if __name__ == "__main__":
    main()
