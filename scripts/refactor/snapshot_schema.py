"""Snapshot registered ORM structure without connecting to a database."""
from __future__ import annotations

import contextlib
import json
import sys


def default_value(default):
    if default is None:
        return None
    if hasattr(default, "sqltext"):
        return {"computed": str(default.sqltext), "persisted": default.persisted}
    value = getattr(default, "arg", default)
    if callable(value):
        return {"callable": value.__qualname__}
    return str(value)


def constraint_snapshot(constraint):
    value = {
        "type": type(constraint).__name__,
        "name": constraint.name,
        "columns": [column.name for column in constraint.columns],
        "deferrable": constraint.deferrable,
        "initially": constraint.initially,
    }
    if hasattr(constraint, "sqltext"):
        value["sqltext"] = str(constraint.sqltext)
    if hasattr(constraint, "elements"):
        value["references"] = [
            {"target": item.target_fullname, "ondelete": item.ondelete,
             "onupdate": item.onupdate, "match": item.match}
            for item in constraint.elements
        ]
    return value


def snapshot(metadata):
    return {
        table.fullname: {
            "columns": [
                {
                    "name": column.name,
                    "type": str(column.type),
                    "nullable": column.nullable,
                    "primary_key": column.primary_key,
                    "default": default_value(column.default),
                    "server_default": default_value(column.server_default),
                    "onupdate": default_value(column.onupdate),
                    "server_onupdate": default_value(column.server_onupdate),
                    "foreign_keys": sorted(
                        (fk.target_fullname, fk.ondelete, fk.onupdate)
                        for fk in column.foreign_keys
                    ),
                }
                for column in table.columns
            ],
            "constraints": sorted(
                (constraint_snapshot(item) for item in table.constraints),
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
            "indexes": sorted(
                (index.name, bool(index.unique), tuple(str(e) for e in index.expressions))
                for index in table.indexes
            ),
        }
        for table in sorted(metadata.tables.values(), key=lambda item: item.fullname)
    }


def main():
    with contextlib.redirect_stdout(sys.stderr):
        # The app is the composition root for installed feature schemas.
        from api.app import app  # noqa: F401
        from core.db.engine import Base
    print(json.dumps(snapshot(Base.metadata), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
