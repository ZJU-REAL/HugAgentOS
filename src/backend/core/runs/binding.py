"""Lifetime and fault handling for owned non-chat agent runs."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any, Mapping, Optional

from core.db.engine import SessionLocal
from core.db.models import ChatRun, ChatSession
from core.db.threads import run_db
from core.runs.journal_domain import DurableRunBinding, RunAlreadyExists, RunJournalError, RunLeaseLost
from sqlalchemy.orm import sessionmaker


def _admit(
    session_factory: sessionmaker,
    journal,
    *,
    user_id: str,
    chat_id: str,
    kind: str,
    external_id: str,
    run_id: str,
    owner: str,
    request_payload: Optional[Mapping[str, Any]],
    recovery_snapshot: Optional[Mapping[str, Any]],
    lease_seconds: int,
    deterministic: bool,
) -> None:
    """Create the run's chat if needed, accept the run and claim its lease."""
    with session_factory() as db:
        session = db.get(ChatSession, chat_id)
        if session is None:
            db.add(
                ChatSession(
                    chat_id=chat_id,
                    user_id=user_id,
                    title=f"{kind} run",
                )
            )
            db.commit()
        elif session.user_id != user_id:
            raise RunJournalError("durable run chat belongs to another user")

    snapshot = {"kind": kind, "external_id": external_id}
    snapshot.update(dict(recovery_snapshot or {}))
    try:
        journal.accept(
            run_id=run_id,
            message_id=f"msg_{uuid.uuid4().hex}",
            chat_id=chat_id,
            user_id=user_id,
            request_payload={
                "kind": kind,
                "external_id": external_id,
                **dict(request_payload or {}),
            },
            recovery_snapshot=snapshot,
        )
    except Exception as exc:
        # Deterministic identities are a tiny database-backed admission lock.
        # The primary key resolves the check-then-create race across processes;
        # only translate an error when that exact row now exists so unrelated
        # database failures retain their original signal.
        if deterministic:
            with session_factory() as db:
                if db.get(ChatRun, run_id) is not None:
                    raise RunAlreadyExists(run_id) from exc
        raise
    if not journal.claim(run_id, owner=owner, lease_seconds=lease_seconds):
        raise RunLeaseLost(run_id)


@asynccontextmanager
async def durable_run_binding(
    *,
    user_id: str,
    chat_id: Optional[str],
    kind: str,
    external_id: str,
    request_payload: Optional[Mapping[str, Any]] = None,
    recovery_snapshot: Optional[Mapping[str, Any]] = None,
    session_factory: sessionmaker = SessionLocal,
    lease_seconds: int = 300,
    heartbeat_interval: Optional[float] = None,
    binding_run_id: Optional[str] = None,
):
    """Give non-chat Agent entry points the same durable run/heartbeat contract."""

    from core.runs.journal import RunJournal

    actual_chat_id = str(chat_id or f"internal_{uuid.uuid4().hex[:24]}")
    run_id = str(binding_run_id or f"run_{uuid.uuid4().hex}")
    owner = f"{kind}:{uuid.uuid4().hex}"
    journal = RunJournal(session_factory)
    await run_db(
        _admit,
        session_factory,
        journal,
        user_id=user_id,
        chat_id=actual_chat_id,
        kind=kind,
        external_id=external_id,
        run_id=run_id,
        owner=owner,
        request_payload=request_payload,
        recovery_snapshot=recovery_snapshot,
        lease_seconds=lease_seconds,
        deterministic=bool(binding_run_id),
    )

    lost = asyncio.Event()
    worker_task = asyncio.current_task()
    if worker_task is None:  # pragma: no cover - async context always has a task
        raise RuntimeError("durable run binding has no asyncio task")

    async def _heartbeat() -> None:
        interval = heartbeat_interval
        if interval is None:
            interval = max(1.0, min(30.0, lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await asyncio.to_thread(
                    journal.renew,
                    run_id,
                    owner=owner,
                    lease_seconds=lease_seconds,
                )
            except Exception:  # noqa: BLE001 - failed heartbeat means lost ownership
                renewed = False
            if not renewed:
                lost.set()
                worker_task.cancel()
                return

    heartbeat = asyncio.create_task(_heartbeat())
    from core.llm.context.safety import request_admission_scope

    with request_admission_scope(
        context={"run_id": run_id, "journal_owner": owner},
    ) as admission:
        try:
            yield DurableRunBinding(run_id=run_id, owner=owner, chat_id=actual_chat_id)
            if lost.is_set():
                raise RunLeaseLost(run_id)
            admission.check()
        except asyncio.CancelledError:
            if not lost.is_set():
                await run_db(
                    journal.complete,
                    run_id,
                    owner=owner,
                    status="cancelled",
                    failure_reason=f"{kind} invocation cancelled",
                )
            raise
        except Exception as exc:
            from core.runs.tool_effects.errors import find_tool_outcome_unknown

            unknown = find_tool_outcome_unknown(exc)
            if admission.stopped is not None:
                from core.chat.compaction.budget_checkpoint import ContextBudgetJournal

                await run_db(
                    ContextBudgetJournal(session_factory).pause_context,
                    {"run_id": run_id, "journal_owner": owner},
                    admission.stopped,
                    admission.checkpoint,
                )
            elif unknown is not None:
                await run_db(
                    journal.needs_attention,
                    run_id,
                    owner=owner,
                    reason=f"tool outcome pending policy recovery: {unknown}",
                )
            else:
                await run_db(
                    journal.complete,
                    run_id,
                    owner=owner,
                    status="failed",
                    failure_reason=f"{type(exc).__name__}: {exc}"[:2000],
                )
            raise
        else:
            await run_db(journal.complete, run_id, owner=owner, status="completed")
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
