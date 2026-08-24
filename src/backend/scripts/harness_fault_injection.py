#!/usr/bin/env python3
"""Run the Harness v2 process-crash recovery matrix against ephemeral SQLite.

This is intentionally a destructive *test-only* utility.  It creates one new
database per safe point below ``--state-dir``, starts a worker process, waits
until the durable safe point is committed, sends that process ``SIGKILL``, and
then starts a fresh recovery process against the same database.

The utility refuses non-SQLite databases and existing phase databases so it
cannot accidentally be pointed at a development or production database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

SAFE_POINTS = (
    "pending",
    "model_before",
    "model_after",
    "tool_intent",
    "tool_unknown",
    "message_committed",
    "compacting",
    "memory_outbox",
)
RUN_SAFE_POINTS = {
    "pending",
    "model_before",
    "model_after",
    "tool_intent",
    "tool_unknown",
    "message_committed",
}


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False), flush=True)


def _phase_database(state_dir: str, phase: str) -> Path:
    root = Path(state_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"harness-fault-{phase}.sqlite"


def _configure_database(state_dir: str, phase: str) -> Path:
    database = _phase_database(state_dir, phase)
    os.environ["DATABASE_URL"] = f"sqlite:///{database}"
    os.environ["SQLITE_FALLBACK_URL"] = f"sqlite:///{database}"
    os.environ["REDIS_URL"] = "memory://"
    os.environ["MEMORY_OUTBOX_LEASE_SECONDS"] = "1"
    os.environ["MEMORY_OUTBOX_RETRY_BASE_SECONDS"] = "0"
    os.environ["MEMORY_AUDIT_ENABLED"] = "false"
    return database


def _initialize_schema() -> None:
    from core.db import models as _models  # noqa: F401
    from core.db.engine import Base, engine

    Base.metadata.create_all(engine)


def _ensure_chat(chat_id: str, user_id: str) -> None:
    from core.db.engine import SessionLocal
    from core.db.models import ChatSession

    with SessionLocal() as db:
        if db.get(ChatSession, chat_id) is None:
            db.add(
                ChatSession(chat_id=chat_id, user_id=user_id, title="fault injection")
            )
            db.commit()


def _run_snapshot(phase: str) -> dict[str, Any]:
    return {
        "kind": "chat",
        "worker_args": {
            "session_messages": [{"role": "user", "content": f"recover {phase}"}],
            "effective_user_message": f"recover {phase}",
            "raw_user_message": f"recover {phase}",
            "context": {},
            "model_name": "fault-injection-model",
        },
    }


def _arm_run_phase(phase: str, lease_seconds: int) -> dict[str, Any]:
    from core.db.models import ChatMessage
    from core.services.run_journal import RunJournal

    chat_id = f"chat-{phase}"
    user_id = "fault-user"
    run_id = f"run-{phase}"
    message_id = f"assistant-{phase}"
    _ensure_chat(chat_id, user_id)
    journal = RunJournal()
    journal.accept(
        run_id=run_id,
        message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        request_payload={"kind": "chat", "fault_phase": phase},
        recovery_snapshot=_run_snapshot(phase),
    )
    if phase == "pending":
        return {"run_id": run_id}

    owner = f"fault-worker:{os.getpid()}"
    if not journal.claim(run_id, owner=owner, lease_seconds=lease_seconds):
        raise RuntimeError(f"failed to claim {run_id}")

    if phase == "model_before":
        journal.append_operation(
            run_id,
            owner=owner,
            operation_type="model_call_prepared",
            phase="pre_model",
            safety="replayable",
            payload={"fault_phase": phase},
        )
    elif phase == "model_after":
        journal.save_snapshot(
            run_id,
            owner=owner,
            phase="model_completed",
            safety="replayable",
            snapshot={
                "assistant_content": "durable model output",
                "message_id": message_id,
                "model_name": "fault-injection-model",
                "usage": {"input_tokens": 3, "output_tokens": 4},
                "context": {},
            },
        )
    elif phase in {"tool_intent", "tool_unknown"}:
        from core.services.tool_effect_ledger import ToolEffectJournal

        effects = ToolEffectJournal()
        recovery_policy = "replay_safe" if phase == "tool_intent" else "never_replay"
        decision = effects.begin_intent(
            run_id=run_id,
            owner=owner,
            claim_owner=f"tool-claim:{os.getpid()}",
            tool_call_id=f"call-{phase}",
            tool_name=f"fault-{phase}",
            args={"path": "/ephemeral/fault-input"},
            recovery_policy=recovery_policy,
            lease_seconds=lease_seconds,
        )
        if phase == "tool_unknown":
            effects.mark_unknown(
                decision.effect_id,
                owner=owner,
                claim_owner=f"tool-claim:{os.getpid()}",
                reason="fault injection persisted an unknown write outcome",
            )
        return {"run_id": run_id, "effect_id": decision.effect_id}
    elif phase == "message_committed":

        def _commit_message(db) -> None:
            db.add(
                ChatMessage(
                    message_id=message_id,
                    chat_id=chat_id,
                    role="assistant",
                    content="durable assistant message",
                )
            )

        journal.append_operation(
            run_id,
            owner=owner,
            operation_type="message_committed",
            phase="message_committed",
            safety="side_effect_committed",
            payload={"message_id": message_id},
            commit_effect=_commit_message,
        )
    return {"run_id": run_id}


def _arm_compaction(lease_seconds: int) -> dict[str, Any]:
    from core.db.engine import SessionLocal
    from core.services.chat_service import ChatService

    chat_id = "chat-compacting"
    _ensure_chat(chat_id, "fault-user")
    with SessionLocal() as db:
        service = ChatService(db)
        service.add_message(
            chat_id=chat_id, role="user", content="question before crash"
        )
        service.add_message(
            chat_id=chat_id, role="assistant", content="answer before crash"
        )
        snapshot = service.acquire_compaction_snapshot(
            chat_id,
            owner=f"compactor:{os.getpid()}",
            lease_seconds=lease_seconds,
        )
        if snapshot is None:
            raise RuntimeError("failed to acquire compaction snapshot")
    # This arrives after the fixed source watermark, while the dead worker
    # would have been summarizing.  The restart must not lose it.
    with SessionLocal() as db:
        ChatService(db).add_message(
            chat_id=chat_id,
            role="user",
            content="tail committed during dead compaction",
        )
    return {
        "chat_id": chat_id,
        "covered_seq": snapshot.covered_seq,
        "source_message_ids": list(snapshot.source_message_ids),
    }


def _arm_memory_outbox() -> dict[str, Any]:
    from core.memory.context import MemoryContext
    from core.memory.outbox import _claim_specific, enqueue_profile_edit_job

    ctx = MemoryContext(
        user_id="fault-user",
        workspace_id="fault-workspace",
        chat_id="chat-memory-outbox",
        write_enabled=True,
        message_id="message-memory-outbox",
    )
    job_id = enqueue_profile_edit_job(
        ctx,
        "preference.language",
        "Chinese",
        operation_id="fault-memory-operation",
    )
    worker_id = f"memory-worker:{os.getpid()}"
    if not _claim_specific(job_id, worker_id):
        raise RuntimeError("failed to claim memory outbox row")
    return {"job_id": job_id, "worker_id": worker_id}


def _arm(args: argparse.Namespace) -> int:
    database = _configure_database(args.state_dir, args.phase)
    if database.exists():
        raise RuntimeError(f"refusing to overwrite existing fault database: {database}")
    _initialize_schema()
    if args.phase in RUN_SAFE_POINTS:
        details = _arm_run_phase(args.phase, args.lease_seconds)
    elif args.phase == "compacting":
        details = _arm_compaction(args.lease_seconds)
    elif args.phase == "memory_outbox":
        details = _arm_memory_outbox()
    else:  # pragma: no cover - argparse constrains the value
        raise ValueError(args.phase)
    _emit({"state": "armed", "phase": args.phase, **details})
    while True:
        signal.pause()


def _recover_run_phase(phase: str) -> dict[str, Any]:
    from core.db.engine import SessionLocal
    from core.db.models import ChatMessage, ChatRun, ToolEffectLedger
    from core.services.run_journal import RunJournal

    run_id = f"run-{phase}"
    tool_actions: list[str] = []
    if phase == "tool_intent":
        from core.services.tool_effect_ledger import (
            ToolEffectJournal,
            ToolRecoveryRegistry,
            recover_incomplete_tool_effects,
        )

        registry = ToolRecoveryRegistry()
        registry.register(
            "fault-tool_intent",
            "replay_safe",
            replayer=lambda intent: {
                "replayed": True,
                "effect_id": intent.effect_id,
            },
        )
        decisions = asyncio.run(
            recover_incomplete_tool_effects(
                journal=ToolEffectJournal(),
                registry=registry,
                lease_seconds=1,
            )
        )
        tool_actions = [decision.action for decision in decisions]

    journal_decisions = RunJournal().recover()
    matching = [decision for decision in journal_decisions if decision.run_id == run_id]
    run_action = matching[0].action if matching else None

    if phase == "model_after" and matching:
        from orchestration.chat_run_executor import _commit_recovered_chat_snapshot

        asyncio.run(_commit_recovered_chat_snapshot(matching[0]))

    with SessionLocal() as db:
        run = db.get(ChatRun, run_id)
        messages = (
            db.query(ChatMessage)
            .filter(ChatMessage.chat_id == f"chat-{phase}")
            .order_by(ChatMessage.chat_seq)
            .all()
        )
        effects = (
            db.query(ToolEffectLedger)
            .filter(ToolEffectLedger.run_id == run_id)
            .order_by(ToolEffectLedger.event_id)
            .all()
        )
        return {
            "phase": phase,
            "run_action": run_action,
            "run_status": run.status if run is not None else None,
            "run_phase": run.run_phase if run is not None else None,
            "tool_actions": tool_actions,
            "tool_events": [row.event_type for row in effects],
            "message_contents": [row.content for row in messages],
        }


def _recover_compaction() -> dict[str, Any]:
    from core.db.engine import SessionLocal
    from core.llm import compaction as compaction_ir
    from core.services.chat_service import ChatService

    chat_id = "chat-compacting"
    owner = f"recovery-compactor:{os.getpid()}"
    with SessionLocal() as db:
        service = ChatService(db)
        snapshot = service.acquire_compaction_snapshot(
            chat_id,
            owner=owner,
            lease_seconds=30,
        )
        if snapshot is None:
            raise RuntimeError("expired compaction lease was not recoverable")
        source_ids = list(snapshot.source_message_ids)
        service.commit_compaction_checkpoint(
            snapshot,
            owner=owner,
            summary_text=compaction_ir.format_summary_text("recovered summary"),
            replacement_history=[
                {
                    "role": "user",
                    "content": compaction_ir.format_summary_text("recovered summary"),
                }
            ],
            replacement_manifest={
                "fault_injection": True,
                "source_message_ids": source_ids,
            },
        )
        checkpoint = service.get_latest_compaction_checkpoint(chat_id)
        history = service.list_all_messages(chat_id, "fault-user") or []
        return {
            "phase": "compacting",
            "checkpoint_active": checkpoint is not None,
            "covered_seq": (checkpoint.extra_data or {}).get("covered_seq")
            if checkpoint
            else None,
            "source_message_ids": source_ids,
            "source_contents": [row.content for row in history if row.role != "system"],
        }


def _recover_memory_outbox() -> dict[str, Any]:
    from core.db.engine import SessionLocal
    from core.db.models import MemoryOutbox, ProfileMemory
    from core.memory.outbox import drain_outbox

    processed = asyncio.run(
        drain_outbox(max_jobs=10, worker_id="restart-memory-worker")
    )
    processed_again = asyncio.run(
        drain_outbox(max_jobs=10, worker_id="restart-memory-worker-2")
    )
    with SessionLocal() as db:
        job = (
            db.query(MemoryOutbox).filter(MemoryOutbox.job_kind == "profile_edit").one()
        )
        profile = db.get(ProfileMemory, ("fault-user", "fault-workspace"))
        return {
            "phase": "memory_outbox",
            "processed": processed,
            "processed_again": processed_again,
            "job_status": job.status,
            "job_attempts": job.attempts,
            "profile_revision": profile.revision if profile is not None else None,
            "profile_content": profile.content_md if profile is not None else None,
            "receipt_count": len(profile.effect_receipts or {})
            if profile is not None
            else 0,
        }


def _recover(args: argparse.Namespace) -> int:
    database = _configure_database(args.state_dir, args.phase)
    if not database.exists():
        raise RuntimeError(f"fault database does not exist: {database}")
    if args.phase in RUN_SAFE_POINTS:
        result = _recover_run_phase(args.phase)
    elif args.phase == "compacting":
        result = _recover_compaction()
    elif args.phase == "memory_outbox":
        result = _recover_memory_outbox()
    else:  # pragma: no cover - argparse constrains the value
        raise ValueError(args.phase)
    _emit({"state": "recovered", **result})
    return 0


def _read_armed(
    proc: subprocess.Popen[str], phase: str, timeout: float
) -> dict[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("fault worker stdout was not captured")
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        output: list[str] = []
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                remainder = proc.stdout.read()
                if remainder:
                    output.append(remainder)
                stderr = proc.stderr.read() if proc.stderr is not None else ""
                raise RuntimeError(
                    f"fault worker {phase} exited before arming: "
                    f"stdout={''.join(output)!r} stderr={stderr!r}"
                )
            events = selector.select(timeout=min(0.25, deadline - time.monotonic()))
            if not events:
                continue
            line = proc.stdout.readline()
            if not line:
                continue
            output.append(line)
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(payload, dict)
                and payload.get("state") == "armed"
                and payload.get("phase") == phase
            ):
                return {str(key): value for key, value in payload.items()}
    raise TimeoutError(f"fault worker {phase} did not arm within {timeout}s")


def _last_json(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError(f"subprocess emitted no JSON payload: {stdout!r}")


def _assert_matrix(results: dict[str, dict[str, Any]]) -> None:
    assert results["pending"]["run_action"] == "resume"
    assert results["model_before"]["run_action"] == "resume"
    assert results["model_after"]["run_action"] == "resume_from_snapshot"
    assert results["model_after"]["run_status"] == "completed"
    assert results["model_after"]["message_contents"] == ["durable model output"]
    assert results["tool_intent"]["tool_actions"] == ["replayed"]
    assert results["tool_intent"]["tool_events"] == [
        "intent",
        "recovery_claim",
        "result",
    ]
    assert results["tool_intent"]["run_status"] == "needs_attention"
    assert results["tool_unknown"]["run_status"] == "needs_attention"
    assert results["tool_unknown"]["tool_events"] == ["intent", "unknown_outcome"]
    assert results["message_committed"]["run_action"] == "completed"
    assert results["message_committed"]["run_status"] == "completed"
    assert results["message_committed"]["message_contents"] == [
        "durable assistant message"
    ]
    assert results["compacting"]["checkpoint_active"] is True
    assert len(results["compacting"]["source_message_ids"]) == 3
    assert (
        "tail committed during dead compaction"
        in results["compacting"]["source_contents"]
    )
    assert results["memory_outbox"]["processed"] == 1
    assert results["memory_outbox"]["processed_again"] == 0
    assert results["memory_outbox"]["job_status"] == "succeeded"
    assert results["memory_outbox"]["profile_content"] == (
        "- **preference.language**: Chinese"
    )
    assert results["memory_outbox"]["receipt_count"] == 1


def _matrix(args: argparse.Namespace) -> int:
    if not hasattr(signal, "SIGKILL"):
        raise RuntimeError("the process-crash matrix requires POSIX SIGKILL")
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    if any(state_dir.iterdir()):
        raise RuntimeError(f"state directory must be empty: {state_dir}")

    script = Path(__file__).resolve()
    killed: dict[str, dict[str, Any]] = {}
    for phase in SAFE_POINTS:
        command = [
            sys.executable,
            str(script),
            "arm",
            "--state-dir",
            str(state_dir),
            "--phase",
            phase,
            "--lease-seconds",
            str(args.lease_seconds),
        ]
        proc = subprocess.Popen(
            command,
            cwd=str(script.parent.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            armed = _read_armed(proc, phase, args.timeout)
            os.kill(proc.pid, signal.SIGKILL)
            return_code = proc.wait(timeout=5)
            if return_code != -signal.SIGKILL:
                raise RuntimeError(
                    f"fault worker {phase} was not killed by SIGKILL: {return_code}"
                )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        killed[phase] = armed

    # All leases were armed with the same short duration.  Wait once after all
    # workers are dead, then recover every database in a genuinely fresh process.
    time.sleep(args.lease_seconds + 0.25)
    results: dict[str, dict[str, Any]] = {}
    for phase in SAFE_POINTS:
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "recover",
                "--state-dir",
                str(state_dir),
                "--phase",
                phase,
            ],
            cwd=str(script.parent.parent),
            capture_output=True,
            text=True,
            timeout=args.timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"recovery process {phase} failed: "
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
            )
        results[phase] = _last_json(completed.stdout)

    _assert_matrix(results)
    _emit(
        {
            "state": "matrix_passed",
            "signal": "SIGKILL",
            "killed_phases": list(killed),
            "results": results,
        }
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    matrix = subparsers.add_parser("matrix", help="kill and recover all safe points")
    matrix.add_argument(
        "--state-dir",
        default=str(Path(tempfile.gettempdir()) / "hugagent-harness-fault-matrix"),
    )
    matrix.add_argument("--lease-seconds", type=int, default=1)
    matrix.add_argument("--timeout", type=float, default=30.0)
    matrix.set_defaults(handler=_matrix)

    arm = subparsers.add_parser("arm", help=argparse.SUPPRESS)
    arm.add_argument("--state-dir", required=True)
    arm.add_argument("--phase", choices=SAFE_POINTS, required=True)
    arm.add_argument("--lease-seconds", type=int, default=1)
    arm.set_defaults(handler=_arm)

    recover = subparsers.add_parser("recover", help=argparse.SUPPRESS)
    recover.add_argument("--state-dir", required=True)
    recover.add_argument("--phase", choices=SAFE_POINTS, required=True)
    recover.set_defaults(handler=_recover)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if getattr(args, "lease_seconds", 1) < 1:
        raise ValueError("lease seconds must be at least 1")
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
