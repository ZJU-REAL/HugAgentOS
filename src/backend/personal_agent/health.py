"""HealthBoard: one row per background loop, written only by the loop that registered it.

A row is keyed by (name, owner). ``owner`` is the process identity, so two
backend processes each keep their own rows and never write each other's.
The loop writes ``beat`` every cycle, ``ok`` after a successful cycle and
``error(detail)`` after a failed one. Three failures in a row turn the row to
``error``; one success turns it back to ``ok``. A row whose beat is older than
three intervals is reported as stale by :func:`board`, which catches a loop
that stopped without raising.
"""
import logging
import time
from datetime import timedelta

from sqlalchemy import Column, Float, Integer, String, delete, select

from core.db import engine
from core.db.engine import Base
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now

log = logging.getLogger(__name__)
FAILURES_FOR_ERROR = 3
STALE_INTERVALS = 3
# Fast loops (the worker polls every 0.3 s) beat at most this often.
MIN_BEAT_SEC = 5.0


class PersonalRuntimeHealth(Base):
    __tablename__ = "personal_runtime_health"
    name = Column(String(100), primary_key=True)
    owner = Column(String(100), primary_key=True)
    status = Column(String(16), nullable=False, default="ok")
    detail = Column(String(500))
    interval_sec = Column(Float, nullable=False, default=0)
    failures = Column(Integer, nullable=False, default=0)
    failing_since = Column(UTCDateTime(timezone=True))
    beat_at = Column(UTCDateTime(timezone=True))
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)


class HealthEntry:
    """The single writer of one HealthBoard row. Writes never raise into the loop."""

    def __init__(self, name, owner, interval):
        self.name, self.owner = name, owner
        self.interval = max(float(interval), MIN_BEAT_SEC)
        self.failures = 0
        self.last_beat = 0.0
        self.status = "ok"

    def register(self):
        """Create this row and drop rows of the same name whose owner stopped beating."""
        def write(db):
            now = utc_now()
            for row in db.scalars(select(PersonalRuntimeHealth).where(
                    PersonalRuntimeHealth.name == self.name, PersonalRuntimeHealth.owner != self.owner)).all():
                if row.beat_at is None or now - row.beat_at > timedelta(seconds=STALE_INTERVALS * row.interval_sec):
                    db.delete(row)
            row = db.get(PersonalRuntimeHealth, (self.name, self.owner)) or PersonalRuntimeHealth(name=self.name, owner=self.owner)
            row.status, row.detail, row.failures, row.failing_since = "ok", None, 0, None
            row.interval_sec, row.beat_at = self.interval, now
            db.merge(row)
        self._write(write)
        self.last_beat = time.monotonic()

    @property
    def beat_due(self):
        return time.monotonic() - self.last_beat >= self.interval / 2

    @property
    def recovering(self):
        return self.failures > 0 or self.status != "ok"

    def beat(self):
        if not self.beat_due:
            return
        self.last_beat = time.monotonic()
        self._update(beat_at=utc_now())

    def ok(self):
        if not self.recovering:
            return
        self.failures, self.status = 0, "ok"
        self._update(status="ok", detail=None, failures=0, failing_since=None)

    def error(self, detail, fatal=False):
        """Record one failure. ``fatal`` (the loop has exited) marks the row at once."""
        self.failures += 1
        values = {"detail": str(detail)[:500], "failures": self.failures}
        if self.failures == 1:
            values["failing_since"] = utc_now()
        if fatal or self.failures >= FAILURES_FOR_ERROR:
            self.status = values["status"] = "error"
        self._update(**values)

    def unregister(self):
        self._write(lambda db: db.execute(delete(PersonalRuntimeHealth).where(
            PersonalRuntimeHealth.name == self.name, PersonalRuntimeHealth.owner == self.owner)))

    def _update(self, **values):
        def write(db):
            row = db.get(PersonalRuntimeHealth, (self.name, self.owner))
            if row is None:
                return
            for key, value in values.items():
                setattr(row, key, value)
        self._write(write)

    def _write(self, fn):
        try:
            with engine.SessionLocal() as db:
                fn(db)
                db.commit()
        except Exception as exc:
            log.debug("Personal health write for %s failed (%s)", self.name, type(exc).__name__)


def board(db):
    """Every row with its effective status: stored ``error`` or a stale beat both read as error."""
    now = utc_now()
    entries = []
    for row in db.scalars(select(PersonalRuntimeHealth).order_by(PersonalRuntimeHealth.name, PersonalRuntimeHealth.owner)).all():
        stale = row.beat_at is None or now - row.beat_at > timedelta(seconds=STALE_INTERVALS * row.interval_sec)
        entries.append({
            "name": row.name, "owner": row.owner,
            "status": "error" if stale or row.status == "error" else row.status,
            "stale": stale, "detail": row.detail or ("no heartbeat" if stale else None),
            "failures": row.failures, "failing_since": row.failing_since and row.failing_since.isoformat(),
            "interval_sec": row.interval_sec, "beat_at": row.beat_at and row.beat_at.isoformat(),
        })
    return entries
