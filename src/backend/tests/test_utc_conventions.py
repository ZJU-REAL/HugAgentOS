"""UTC persistence and API clocks must not depend on the host timezone."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Column, DateTime, Integer, TIMESTAMP, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from core.db.utc_datetime import UTCDateTime
from core.infra.time import as_utc, utc_now
from core.infra.responses import error_response, success_response


@pytest.mark.parametrize("zone", ["UTC", "Asia/Shanghai", "America/Los_Angeles"])
def test_response_epoch_is_host_timezone_independent(zone):
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        before = int(time.time() * 1000)
        ok = success_response()["timestamp"]
        error = json.loads(error_response(40000, "test").body)["timestamp"]
        after = int(time.time() * 1000)
        assert before <= ok <= after
        assert before <= error <= after
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_sqlite_round_trip_preserves_instant_and_timezone():
    base = declarative_base()
    class Record(base):
        __tablename__ = "utc_record"
        id = Column(Integer, primary_key=True)
        stamp = Column(UTCDateTime(), default=utc_now, onupdate=utc_now)
    engine = create_engine("sqlite://")
    base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    instant = datetime(2026, 9, 28, 14, 6, tzinfo=timezone(timedelta(hours=8)))
    with factory() as db:
        row = Record(stamp=instant)
        db.add(row)
        db.commit()
        assert row.stamp == instant
        assert row.stamp.utcoffset() == timedelta(0)
        row.stamp = datetime(2026, 9, 28, 6, 6)
        db.commit()
        assert row.stamp == instant
    engine.dispose()


def test_all_model_timestamp_defaults_are_aware():
    from core.db.engine import Base
    from core.db import models  # noqa: F401
    checked = 0
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, DateTime), str(column)
            if not isinstance(column.type, UTCDateTime):
                continue
            for default in (column.default, column.onupdate):
                if default is not None and default.is_callable:
                    value = default.arg(None)
                    assert value.tzinfo is not None, str(column)
                    assert value.utcoffset() == timedelta(0), str(column)
                    checked += 1
    assert checked > 100


def test_utc_normalization_preserves_offsets():
    assert utc_now().utcoffset() == timedelta(0)
    assert as_utc(datetime(2026, 1, 1)).tzinfo == timezone.utc
    assert as_utc(datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))) == datetime(2026, 1, 1, tzinfo=timezone.utc)



def test_utc_type_preserves_database_schema():
    from sqlalchemy.dialects import postgresql, sqlite
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        assert UTCDateTime().compile(dialect=dialect) == TIMESTAMP(timezone=True).compile(dialect=dialect)


def test_runtime_has_no_naive_utc_clock():
    import ast
    from pathlib import Path
    root = Path(__file__).parents[1]
    for directory in ("core", "api", "edition_ee", "orchestration"):
        for path in (root / directory).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Attribute) and node.attr == "utcnow":
                    pytest.fail(f"Use aware UTC in {path.relative_to(root)}:{node.lineno}")



def test_database_connection_and_migration_rendering():
    from core.db.connection import utc_connect_args
    from core.db.utc_datetime import render_utc_type
    assert utc_connect_args("postgresql+psycopg://localhost/test") == {"options": "-c timezone=UTC"}
    assert utc_connect_args("sqlite://") == {}
    assert render_utc_type("type", UTCDateTime(), None) == "sa.TIMESTAMP(timezone=True)"
    assert render_utc_type("type", Integer(), None) is False
