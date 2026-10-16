"""Real PostgreSQL locks, concurrent writers, CAS publication and repair rollback."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from core.db.models import Artifact, UserFolder, UserShadow
from core.infra.time import utc_now
from core.myspace import mirror
from core.myspace import legacy_paths
from core.services.folder_batch import lock_folder_owner
from core.space_sync.content import register
from tests.space_sync.pg_sync_support import seed, await_event


def test_slow_upload_cannot_block_an_unrelated_file(postgres_space, monkeypatch):
    space = postgres_space
    slow, release = threading.Event(), threading.Event()
    upload = space.storage.upload_bytes

    def delayed(data, key):
        if data == b"slow":
            slow.set()
            await_event(release)
        return upload(data, key)

    monkeypatch.setattr(space.storage, "upload_bytes", delayed)
    (space.root / "slow.txt").write_bytes(b"slow")
    (space.root / "good.txt").write_bytes(b"good")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(register, "alice", mirror.mirror_entry("alice", "slow.txt"))
        try:
            await_event(slow)
            good = pool.submit(register, "alice", mirror.mirror_entry("alice", "good.txt"))
            result = good.result(timeout=2)
            assert space.storage.download_bytes(result["storage_key"]) == b"good"
            assert not first.done()
        finally:
            release.set()
        first.result(timeout=5)


def test_concurrent_same_path_has_one_identity_and_one_referenced_object(
    postgres_space, monkeypatch
):
    space = postgres_space
    path = space.root / "same.txt"
    path.write_bytes(b"same")
    barrier = threading.Barrier(2, timeout=5)
    uploaded, deleted = [], []
    upload, delete = space.storage.upload_bytes, space.storage.delete

    def stage(data, key):
        url = upload(data, key)
        uploaded.append(key)
        barrier.wait()
        return url

    def discard(key):
        deleted.append(key)
        return delete(key)

    monkeypatch.setattr(space.storage, "upload_bytes", stage)
    monkeypatch.setattr(space.storage, "delete", discard)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(register, "alice", mirror.mirror_entry("alice", "same.txt"))
            for _ in range(2)
        ]
        results = []
        for task in futures:
            try:
                results.append(task.result(timeout=8))
            except HTTPException as exc:
                assert exc.status_code == 409
                results.append(exc)
    assert sum(isinstance(result, dict) for result in results) == 1
    with space.factory() as db:
        rows = db.query(Artifact).filter_by(user_id="alice", deleted_at=None).all()
        assert len(rows) == 1
        assert rows[0].filename == "same.txt"
        assert set(uploaded) - set(deleted) == {rows[0].storage_key}
    assert path.read_bytes() == b"same"


def test_cloud_change_during_upload_rejects_stale_publication(postgres_space, monkeypatch):
    space = postgres_space
    identity = seed(space)
    started, release = threading.Event(), threading.Event()
    uploaded, deleted = [], []
    upload, delete = space.storage.upload_bytes, space.storage.delete
    upload(b"cloud-wins", "new-cloud")

    def stage(data, key):
        url = upload(data, key)
        uploaded.append(key)
        started.set()
        await_event(release)
        return url

    def discard(key):
        deleted.append(key)
        return delete(key)

    monkeypatch.setattr(space.storage, "upload_bytes", stage)
    monkeypatch.setattr(space.storage, "delete", discard)
    (space.root / "a.txt").write_bytes(b"unsaved-local")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(register, "alice", mirror.mirror_entry("alice", "a.txt"))
        try:
            await_event(started)
            with space.factory() as writer:
                row = writer.get(Artifact, identity)
                row.storage_key = "new-cloud"
                row.updated_at = utc_now()
                writer.commit()
        finally:
            release.set()
        with pytest.raises(HTTPException) as error:
            pending.result(timeout=5)
        assert error.value.status_code == 409
    with space.factory() as db:
        assert db.get(Artifact, identity).storage_key == "new-cloud"
    assert uploaded == deleted
    assert (space.root / "a.txt").read_bytes() == b"unsaved-local"
    assert space.storage.download_bytes("new-cloud") == b"cloud-wins"


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_maintenance_releases_owner_lock_when_artifact_is_busy(
    postgres_space, monkeypatch, operation
):
    space = postgres_space
    identity = seed(space, name="legacy.txt")
    with space.factory() as db:
        db.execute(
            Artifact.__table__.update()
            .where(Artifact.artifact_id == identity)
            .values(filename="Sub/legacy.txt")
        )
        db.commit()
        plan = legacy_paths.plan_repairs(db, "alice")
        if operation == "rollback":
            legacy_paths.apply_repairs(db, plan)
            db.commit()
    artifact_held, owner_held, inspect_rows = (threading.Event() for _ in range(3))
    lock = legacy_paths.lock_folder_owner

    def maintenance_owner(*args):
        lock(*args)
        owner_held.set()
        await_event(inspect_rows)

    monkeypatch.setattr(legacy_paths, "lock_folder_owner", maintenance_owner)
    writer_pid = []

    def writer():
        with space.factory() as db:
            writer_pid.append(db.execute(text("select pg_backend_pid()")).scalar_one())
            db.query(Artifact).filter_by(artifact_id=identity).with_for_update().one()
            artifact_held.set()
            await_event(owner_held)
            lock_folder_owner(db, UserShadow, "user_id", "alice")
            db.commit()

    def maintenance():
        with space.factory() as db:
            function = (
                legacy_paths.apply_repairs if operation == "apply" else legacy_paths.restore_repairs
            )
            with pytest.raises(ValueError, match="retry") as error:
                function(db, plan)
            assert isinstance(error.value.__cause__, OperationalError)
            assert error.value.__cause__.orig.pgcode == "55P03"
            assert not db.in_transaction()

    with ThreadPoolExecutor(max_workers=2) as pool:
        active_writer = pool.submit(writer)
        await_event(artifact_held)
        active_maintenance = pool.submit(maintenance)
        try:
            await_event(owner_held)
            deadline = time.monotonic() + 2
            blocked = False
            while time.monotonic() < deadline:
                with space.engine.connect() as conn:
                    blocked = conn.execute(
                        text("select cardinality(pg_blocking_pids(:pid)) > 0"),
                        {"pid": writer_pid[0]},
                    ).scalar_one()
                if blocked:
                    break
                time.sleep(0.01)
            assert blocked, "Writer must actually be waiting on maintenance's owner lock"
        finally:
            inspect_rows.set()
        active_maintenance.result(timeout=5)
        active_writer.result(timeout=5)
    with space.factory() as db:
        row = db.get(Artifact, identity)
        expected = "Sub/legacy.txt" if operation == "apply" else "legacy.txt"
        assert row.filename == expected
        assert bool((row.extra_data or {}).get("legacy_path_repair")) == (operation == "rollback")


def test_content_nowait_conflict_preserves_bytes_and_releases_owner(postgres_space):
    space = postgres_space
    identity = seed(space)
    (space.root / "a.txt").write_bytes(b"unsaved")
    with space.factory() as holding:
        holding.query(Artifact).filter_by(artifact_id=identity).with_for_update().one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(register, "alice", mirror.mirror_entry("alice", "a.txt"))
            with pytest.raises(HTTPException) as error:
                pending.result(timeout=2)
            assert error.value.status_code == 409
        with space.factory() as db:
            # A leaked owner lock would make this actual PostgreSQL NOWAIT fail.
            db.query(UserShadow).filter_by(user_id="alice").with_for_update(nowait=True).one()
        holding.rollback()
    assert (space.root / "a.txt").read_bytes() == b"unsaved"


def test_repair_roundtrip_retains_ids_and_storage_on_postgres(postgres_space):
    space = postgres_space
    identity = seed(space, "legacy.txt")
    with space.factory() as db:
        original_key = db.get(Artifact, identity).storage_key
        db.execute(
            Artifact.__table__.update()
            .where(Artifact.artifact_id == identity)
            .values(filename="Deep/Sub/legacy.txt")
        )
        db.commit()
        plan = legacy_paths.plan_repairs(db, "alice")
        assert legacy_paths.apply_repairs(db, plan) == 1
        db.commit()
        assert db.get(Artifact, identity).filename == "legacy.txt"
        assert db.query(UserFolder).filter_by(user_id="alice").count() == 2
        assert legacy_paths.restore_repairs(db, plan) == 1
        db.commit()
        assert db.get(Artifact, identity).filename == "Deep/Sub/legacy.txt"
        assert db.get(Artifact, identity).storage_key == original_key
        assert space.storage.download_bytes(original_key) == b"initial"


def test_move_cloud_verification_cannot_hold_owner_during_network_io(postgres_space, monkeypatch):
    from core.space_sync.events import Change
    from core.space_sync.personal_metadata import apply

    space = postgres_space
    identity = seed(space)
    with space.factory() as db:
        row = db.get(Artifact, identity)
        row.updated_at = utc_now()
        db.commit()
    (space.root / "a.txt").rename(space.root / "renamed.txt")
    (space.root / "good.txt").write_bytes(b"good")
    started, release = threading.Event(), threading.Event()
    download = space.storage.download_bytes

    def delayed(key):
        started.set()
        await_event(release)
        return download(key)

    monkeypatch.setattr(space.storage, "download_bytes", delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        moving = pool.submit(apply, "alice", Change("moved", "a.txt", "renamed.txt"))
        try:
            await_event(started)
            good = pool.submit(register, "alice", mirror.mirror_entry("alice", "good.txt"))
            assert good.result(timeout=2)["name"] == "good.txt"
            assert not moving.done()
        finally:
            release.set()
        assert moving.result(timeout=5)
    with space.factory() as db:
        assert db.get(Artifact, identity).filename == "renamed.txt"


def test_move_cloud_change_during_verification_retains_local_move(postgres_space, monkeypatch):
    from core.space_sync.events import Change
    from core.space_sync.personal_metadata import apply

    space = postgres_space
    identity = seed(space)
    with space.factory() as db:
        row = db.get(Artifact, identity)
        row.updated_at = utc_now()
        db.commit()
    (space.root / "a.txt").rename(space.root / "renamed.txt")
    started, release = threading.Event(), threading.Event()
    download = space.storage.download_bytes

    def delayed(key):
        started.set()
        await_event(release)
        return download(key)

    monkeypatch.setattr(space.storage, "download_bytes", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        moving = pool.submit(apply, "alice", Change("moved", "a.txt", "renamed.txt"))
        try:
            await_event(started)
            with space.factory() as db:
                row = db.get(Artifact, identity)
                row.summary = "Concurrent cloud edit"
                row.updated_at = utc_now()
                db.commit()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="Cloud changed during move"):
            moving.result(timeout=5)
    with space.factory() as db:
        assert db.get(Artifact, identity).filename == "a.txt"
        assert db.get(Artifact, identity).summary == "Concurrent cloud edit"
    assert (space.root / "renamed.txt").read_bytes() == b"initial"


def test_owner_lock_allows_foreign_key_reference_without_deadlock(postgres_space):
    space = postgres_space

    def insert_reference():
        with space.factory() as db:
            db.execute(
                Artifact.__table__.insert().values(
                    artifact_id="fk-reference",
                    user_id="alice",
                    filename="fk.txt",
                    title="fk",
                    type="other",
                    size_bytes=1,
                    storage_key="fk-key",
                    mime_type="text/plain",
                )
            )
            db.commit()

    with space.factory() as holding:
        lock_folder_owner(holding, UserShadow, "user_id", "alice")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(insert_reference).result(timeout=2)
        holding.rollback()


def test_first_index_access_is_safe_across_processes(postgres_space, tmp_path):
    import os
    import subprocess
    import sys

    ready = tmp_path / "ready"
    ready.mkdir()
    go = tmp_path / "go"
    code = """
import sys,time
from pathlib import Path
from core.space_sync.index import remember
ready,go,key = map(Path,sys.argv[1:])
(ready/key).touch()
deadline=time.monotonic()+10
while not go.exists():
    if time.monotonic()>deadline: raise RuntimeError("index start barrier expired")
    time.sleep(.01)
remember("alice",str(key),{"id":str(key)})
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(ready), str(go), str(i)],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(6)
    ]
    try:
        deadline = time.monotonic() + 10
        while len(list(ready.iterdir())) < len(processes) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(list(ready.iterdir())) == len(processes)
        go.touch()
        for process in processes:
            _, error = process.communicate(timeout=10)
            assert process.returncode == 0, error.decode()
        from core.space_sync.index import snapshot

        assert len(snapshot("alice")) == len(processes)
    finally:
        go.touch()
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


@pytest.mark.parametrize("boundary", ["cross_type_target", "destination_parent"])
def test_move_busy_destination_releases_owner_without_waiting(postgres_space, boundary):
    from core.space_sync.events import Change
    from core.space_sync.personal_metadata import apply

    space = postgres_space
    if boundary == "cross_type_target":
        locked_id = seed(space, "Target")
        (space.root / "Old").mkdir()
        apply("alice", Change("created", "Old", directory=True))
        (space.root / "Old").rename(space.root / "Target-dir")
        # The local target is a directory while the cloud target is still a file.
        (space.root / "Target").unlink()
        (space.root / "Target-dir").rename(space.root / "Target")
        change = Change("moved", "Old", "Target", directory=True)
        model, field = Artifact, Artifact.artifact_id
    else:
        seed(space)
        (space.root / "Parent").mkdir()
        apply("alice", Change("created", "Parent", directory=True))
        with space.factory() as db:
            locked_id = db.query(UserFolder).filter_by(name="Parent").one().folder_id
        (space.root / "a.txt").rename(space.root / "Parent/a.txt")
        change = Change("moved", "a.txt", "Parent/a.txt")
        model, field = UserFolder, UserFolder.folder_id
    with space.factory() as holding:
        holding.query(model).filter(field == locked_id).with_for_update().one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(apply, "alice", change)
            with pytest.raises(OperationalError) as error:
                pending.result(timeout=2)
            assert error.value.orig.pgcode == "55P03"
        with space.factory() as db:
            db.query(UserShadow).filter_by(user_id="alice").with_for_update(nowait=True).one()
        holding.rollback()
