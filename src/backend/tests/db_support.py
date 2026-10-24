"""Point the sessions async code opens at a test's database.

Async routes and workers never take ``Depends(get_db)``; their database steps
run through ``core.db.threads.run_in_session``, which opens
``core.db.engine.SessionLocal``. A test that overrides ``get_db`` overrides
this too, so both kinds of route see the same data.
"""

from sqlalchemy.orm import sessionmaker


class _Borrowed:
    """The test's session, handed out as if new; closing it leaves the test's copy open."""

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._session, name)


def share_session(monkeypatch, session):
    """Every step reuses the test's own session, left open like the ``get_db`` override."""
    monkeypatch.setattr("core.db.engine.SessionLocal", lambda: _Borrowed(session))


def share_factory(monkeypatch, factory):
    """Every step opens its own session from the test's factory."""
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)


def share_bind(monkeypatch, bind):
    """Every step opens its own session on the test's engine."""
    share_factory(monkeypatch, sessionmaker(bind=bind))
