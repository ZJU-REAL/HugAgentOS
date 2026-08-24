"""Opt-in PostgreSQL integration for the migrated run journal."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from core.db.models import (
    ChatMessage,
    ChatRun,
    ChatSession,
    ChatSteerQueueItem,
    ToolEffectLease,
    ToolEffectLedger,
    UserShadow,
)
from core.services.chat_service import ChatService
from core.services.run_journal import RunJournal
from core.services.steer_queue import SteerQueue, SteerQueueConflict
from core.services.tool_effect_ledger import ToolEffectJournal
from orchestration.chat_run_executor import _commit_queued_handoff_in_session

POSTGRES_URL = os.getenv("RUN_JOURNAL_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set RUN_JOURNAL_POSTGRES_URL to run PostgreSQL integration",
)


def test_migrated_postgres_schema_and_atomic_claim():
    repo_root = Path(__file__).resolve().parents[4]
    migration_env = dict(os.environ)
    migration_env["DATABASE_URL"] = POSTGRES_URL
    # Accept both a fresh database and a previously migrated reusable test DB.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env=migration_env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "pluginui01"],
        cwd=repo_root,
        env=migration_env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env=migration_env,
        check=True,
        capture_output=True,
        text=True,
    )

    engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
    inspector = inspect(engine)
    assert "chat_run_operations" in inspector.get_table_names()
    run_columns = {column["name"] for column in inspector.get_columns("chat_runs")}
    assert {
        "run_phase",
        "lease_owner",
        "lease_expires_at",
        "operation_seq",
        "snapshot_version",
        "recovery_snapshot",
        "failure_reason",
    }.issubset(run_columns)
    run_indexes = {index["name"] for index in inspector.get_indexes("chat_runs")}
    assert "idx_chat_runs_status_lease" in run_indexes
    assert {"tool_effect_ledger", "tool_effect_leases"}.issubset(inspector.get_table_names())
    assert "chat_steer_queue" in inspector.get_table_names()
    assert "effect_id" in {column["name"] for column in inspector.get_columns("tool_call_logs")}

    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    user_id = f"pg-journal-user-{suffix}"
    chat_id = f"pg-journal-chat-{suffix}"
    run_id = f"pg-journal-run-{suffix}"
    with sessions() as db:
        db.add(UserShadow(user_id=user_id, username=user_id))
        db.add(
            ChatSession(
                chat_id=chat_id,
                user_id=user_id,
                title="journal integration",
            )
        )
        db.commit()

    journal = RunJournal(sessions)
    journal.accept(
        run_id=run_id,
        message_id=f"pg-journal-message-{suffix}",
        chat_id=chat_id,
        user_id=user_id,
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    barrier = Barrier(2)

    def claim(owner: str) -> bool:
        barrier.wait(timeout=5)
        return RunJournal(sessions).claim(
            run_id,
            owner=owner,
            lease_seconds=30,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, ("pg-worker-a", "pg-worker-b"))) == [False, True]

    queued = SteerQueue(sessions).accept(
        target_run_id=run_id,
        chat_id=chat_id,
        user_id=user_id,
        steer_id=f"pg-steer-{suffix}",
        message="postgres durable steer",
        replace_latest=False,
    )
    steer_barrier = Barrier(2)

    def claim_steer(owner: str) -> str | None:
        steer_barrier.wait(timeout=5)
        item = SteerQueue(sessions).claim_next(run_id, owner=owner, lease_seconds=30)
        return item.queue_id if item else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        steer_winners = list(pool.map(claim_steer, ("pg-steer-a", "pg-steer-b")))
    assert steer_winners.count(queued.queue_id) == 1
    assert steer_winners.count(None) == 1
    with sessions() as db:
        assert db.get(ChatSteerQueueItem, queued.queue_id).status == "claimed"

    race_run_id = f"pg-next-race-{suffix}"
    race_journal = RunJournal(sessions)
    race_journal.accept(
        run_id=race_run_id,
        message_id=f"pg-next-race-message-{suffix}",
        chat_id=chat_id,
        user_id=user_id,
        request_payload={"kind": "chat", "message": "race"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    assert race_journal.claim(race_run_id, owner="pg-race-finisher", lease_seconds=60)
    next_race_barrier = Barrier(2)

    def finish_race_run() -> bool:
        next_race_barrier.wait(timeout=5)
        return RunJournal(sessions).complete(
            race_run_id,
            owner="pg-race-finisher",
            status="completed",
            commit_effect=lambda db: _commit_queued_handoff_in_session(
                db,
                source_run_id=race_run_id,
                chat_id=chat_id,
                user_id=user_id,
                session_messages=[{"role": "user", "content": "race"}],
                assistant_content="done",
                context={"chat_id": chat_id, "user_id": user_id},
                model_name="test-model",
            ),
        )

    def accept_racing_next_run() -> str:
        next_race_barrier.wait(timeout=5)
        try:
            SteerQueue(sessions).accept(
                target_run_id=race_run_id,
                chat_id=chat_id,
                user_id=user_id,
                steer_id=f"pg-next-race-steer-{suffix}",
                message="next after race",
                delivery_mode="next_run",
                replace_latest=False,
            )
        except SteerQueueConflict:
            return "SteerQueueConflict"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        finish_future = pool.submit(finish_race_run)
        accept_future = pool.submit(accept_racing_next_run)
        assert finish_future.result() is True
        accept_result = accept_future.result()

    with sessions() as db:
        race_item = (
            db.query(ChatSteerQueueItem)
            .filter(ChatSteerQueueItem.steer_id == f"pg-next-race-steer-{suffix}")
            .one_or_none()
        )
        if race_item is None:
            assert accept_result == "SteerQueueConflict"
        else:
            assert accept_result == "accepted"
            assert race_item.status == "applied"
            assert db.get(ChatRun, race_item.applied_run_id).status == "pending"

    with sessions() as db:
        run_owner = db.get(ChatRun, run_id).lease_owner
    effect_barrier = Barrier(2)

    def prepare_effect(claim_owner: str) -> str:
        effect_barrier.wait(timeout=5)
        return (
            ToolEffectJournal(sessions)
            .begin_intent(
                run_id=run_id,
                owner=run_owner,
                claim_owner=claim_owner,
                tool_call_id="pg-tool-call",
                tool_name="Read",
                args={"path": "/workspace/a.txt"},
                recovery_policy="replay_safe",
                idempotency_key=f"pg-effect-{suffix}",
            )
            .action
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(prepare_effect, ("pg-effect-a", "pg-effect-b"))) == [
            "execute",
            "wait",
        ]

    with sessions() as db:
        effect_id = (
            db.query(ToolEffectLedger.effect_id)
            .filter(
                ToolEffectLedger.run_id == run_id,
                ToolEffectLedger.event_type == "intent",
            )
            .scalar()
        )
        db.query(ChatRun).filter(ChatRun.run_id == run_id).update(
            {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        db.query(ToolEffectLease).filter(ToolEffectLease.effect_id == effect_id).update(
            {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        db.commit()
    recovery_barrier = Barrier(2)

    def claim_effect_recovery(owner: str) -> bool:
        recovery_barrier.wait(timeout=5)
        return (
            ToolEffectJournal(sessions).claim_recovery_intent(
                effect_id,
                recovery_owner=owner,
            )
            is not None
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim_effect_recovery, ("pg-recovery-a", "pg-recovery-b"))) == [
            False,
            True,
        ]

    engine.dispose()


def test_compaction_snapshot_lock_does_not_block_message_writes():
    """The chat-row lock taken while snapshotting must not fence message writes.

    ``ChatService._ensure_message_sequences`` locks the parent ``chat_sessions``
    row before a compaction snapshot reads the history. Inserting into
    ``chat_messages`` takes ``FOR KEY SHARE`` on that same row for the foreign
    key, and ``FOR KEY SHARE`` conflicts with ``FOR UPDATE``. When the snapshot
    used the stronger mode, the run journal's terminal commit blocked behind it;
    because that commit ran on the event loop, the loop could no longer resume
    the coroutine owning the snapshot transaction and the whole process
    deadlocked - every request, stream and health probe stopped answering.
    """
    engine = create_engine(POSTGRES_URL, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    user_id = f"pg-lock-user-{suffix}"
    chat_id = f"pg-lock-chat-{suffix}"
    try:
        with sessions() as db:
            db.add(UserShadow(user_id=user_id, username=user_id))
            db.add(ChatSession(chat_id=chat_id, user_id=user_id, title="lock mode"))
            db.commit()

        holder = sessions()
        try:
            # Reproduce the snapshot's lock exactly, then leave the transaction
            # open the way a suspended coroutine does.
            ChatService(holder)._ensure_message_sequences(chat_id)

            with sessions() as writer:
                # Without the fix this waits forever; the timeout turns the
                # deadlock into a failure the test can actually report.
                writer.execute(text("set local lock_timeout = '4s'"))
                writer.add(
                    ChatMessage(
                        message_id=f"pg-lock-msg-{suffix}",
                        chat_id=chat_id,
                        role="assistant",
                        chat_seq=1,
                        content="written while a snapshot holds the chat row",
                    )
                )
                writer.commit()
        finally:
            holder.rollback()
            holder.close()

        with sessions() as db:
            written = (
                db.query(ChatMessage)
                .filter(ChatMessage.chat_id == chat_id)
                .count()
            )
        assert written == 1
    finally:
        with sessions() as db:
            db.query(ChatMessage).filter(ChatMessage.chat_id == chat_id).delete()
            db.query(ChatSession).filter(ChatSession.chat_id == chat_id).delete()
            db.query(UserShadow).filter(UserShadow.user_id == user_id).delete()
            db.commit()
        engine.dispose()
