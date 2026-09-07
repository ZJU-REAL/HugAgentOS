"""Shared helper: bind the capability registry to a throwaway SQLite index.

Only the four device-capability tables are created, so the fixture is cheap
enough to be module-scoped and autouse where the desktop bridge is exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def bind_capability_index(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch):
    import core.db.models  # noqa: F401 - populate metadata
    from core.capabilities import registry
    from core.db.engine import Base
    from core.db.models import (
        ContentBlock,
        DeviceCapabilityComponent,
        DeviceCapabilityInstallation,
        DeviceCapabilityNamePreference,
        DeviceCapabilityTransaction,
    )

    engine = create_engine(
        f"sqlite:///{tmp_dir / 'capability-index.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(
        engine,
        tables=[
            ContentBlock.__table__,
            DeviceCapabilityInstallation.__table__,
            DeviceCapabilityNamePreference.__table__,
            DeviceCapabilityTransaction.__table__,
            DeviceCapabilityComponent.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(registry, "SessionLocal", factory)
    return engine, factory
