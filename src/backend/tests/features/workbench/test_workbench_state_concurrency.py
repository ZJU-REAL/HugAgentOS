"""One CAS winner and aggregate quota through independent HTTP DB sessions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from sqlalchemy.orm import sessionmaker
from core.db.engine import get_db
from tests.features.workbench.test_workbench_state_routes import client as client, BASE, SCOPE, body
from tests.db_support import share_factory


def test_concurrent_state_creation_has_one_cas_winner(client, db_session, monkeypatch):
    factory = sessionmaker(bind=db_session.get_bind())

    def database():
        with factory() as db:
            yield db

    client.app.dependency_overrides[get_db] = database
    share_factory(monkeypatch, factory)
    barrier = Barrier(2)

    def write(value):
        barrier.wait()
        return client.put(BASE + SCOPE, json=body(payload={"filter": value})).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, "first")
        second = pool.submit(write, "second")
        assert sorted([first.result(), second.result()]) == [200, 409]
    assert client.get(BASE + SCOPE).json()["data"]["revision"] == 1


def test_application_quota_counts_all_workspaces(client, monkeypatch):
    monkeypatch.setenv("WORKBENCH_STATE_APP_BYTES", "256")
    assert client.put(BASE + SCOPE, json=body(payload={"filter": "a" * 180})).status_code == 200
    assert (
        client.put(
            BASE + "?workspace=two&packageRevision=1", json=body(payload={"filter": "b" * 180})
        ).status_code
        == 413
    )
    assert client.get(BASE + SCOPE).json()["data"]["payload"] == {"filter": "a" * 180}
