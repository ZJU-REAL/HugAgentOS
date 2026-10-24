"""Cloud management copies survive delivery retries and never become runnable chats."""

from features.desktop.bridge import identity as bridge_identity

import pytest

from sqlalchemy import create_engine

from sqlalchemy.orm import sessionmaker

@pytest.fixture
def store(tmp_path):
    from features.desktop.models import DesktopOutbox, DesktopRecord, DesktopSyncDevice

    engine = create_engine("sqlite:///" + str(tmp_path / "cloud.db"))
    from core.db.models import UserShadow

    for model in (DesktopRecord, DesktopSyncDevice, DesktopOutbox, UserShadow):
        model.__table__.create(engine)
    return sessionmaker(bind=engine)

@pytest.fixture
def local_store(tmp_path):
    from core.db.models import UserShadow
    from features.desktop.models import DesktopOutbox
    from features.desktop.observability.sync import SOURCES

    engine = create_engine("sqlite:///" + str(tmp_path / "desktop.db"))
    for model in {UserShadow, DesktopOutbox, *(m for m, _ in SOURCES.values())}:
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    return factory
