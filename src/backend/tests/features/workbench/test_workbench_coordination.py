"""Real transaction barriers: independent users proceed, owner/config writers wait."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy.orm import sessionmaker

from features.workbench.coordination import configuration, runtime


def test_runtime_requires_explicit_owner(db_session):
    with pytest.raises(ValueError):
        runtime(db_session, "")


def test_postgres_owner_and_configuration_barriers(db_session):
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL shared/exclusive transaction locks")
    factory = sessionmaker(bind=db_session.get_bind())
    owner = factory()
    runtime(owner, "alice")
    alice_started, config_started = Event(), Event()

    def write_owner(name, started):
        with factory() as db:
            started.set()
            runtime(db, name)
            db.commit()
        return name

    def write_config():
        with factory() as db:
            config_started.set()
            configuration(db)
            db.commit()
        return "config"

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            # Check the other owner before queuing an exclusive config writer.
            assert pool.submit(write_owner, "bob", Event()).result(timeout=3) == "bob"
            alice = pool.submit(write_owner, "alice", alice_started)
            assert alice_started.wait(3)
            config = pool.submit(write_config)
            assert config_started.wait(3)
            with pytest.raises(TimeoutError):
                alice.result(timeout=0.15)
            with pytest.raises(TimeoutError):
                config.result(timeout=0.15)
            owner.commit()
            assert alice.result(timeout=3) == "alice"
            assert config.result(timeout=3) == "config"
    finally:
        owner.close()


def test_postgres_configuration_blocks_new_runtime(db_session):
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("PostgreSQL shared/exclusive transaction locks")
    factory = sessionmaker(bind=db_session.get_bind())
    with factory() as config:
        configuration(config)
        started = Event()

        def writer():
            with factory() as db:
                started.set()
                runtime(db, "alice")
                db.commit()

        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(writer)
            assert started.wait(3)
            try:
                with pytest.raises(TimeoutError):
                    waiting.result(timeout=0.15)
            finally:
                config.commit()
            waiting.result(timeout=3)
