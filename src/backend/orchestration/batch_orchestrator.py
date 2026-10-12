"""Batch execution orchestrator (Phase 2 of the batch flow).

Once the user has confirmed a BatchPlan via the UI dialog, this module
serially iterates over plan.items, spawns a fresh ReActAgent per item
(with batch_runner disabled to prevent recursion), and persists per-item
results back into the plan so the SSE follower can replay them on
reconnect.

Architecture (refresh-survival):

    SSE handler  ──follow──▶  background asyncio.Task   ──writes──▶  DB
        │                       (one per plan_id)                     ▲
        │                              │                              │
        └──── reads stored results + tails new ones ──────────────────┘

The background task is created on the first /stream call for a plan and
keeps running even if the SSE client disconnects (page refresh, tab
switch). When the client reconnects we look up the same task — if it's
still alive we tail; if it finished we replay results from the DB.

Failures are retried up to plan.max_retries with exponential back-off;
exhausted retries are recorded as ``status="skipped"`` and the loop
continues. Setting plan.status='cancelled' in DB stops the loop at the
next iteration boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from core.db.engine import SessionLocal
from core.db.models import BatchPlan, ChatRun
from core.infra.time import utc_now
from orchestration.batch_runner import _run_until_done
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)


# Plan status values — matches the CHECK constraint in
# alembic v2w3x4y5z6a7_add_batch_plans.
PENDING = "pending"
CONFIRMED = "confirmed"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

_TERMINAL = {DONE, CANCELLED, FAILED}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_format(template: str, item: Dict[str, Any]) -> str:
    """Render template via straight ``{key}`` substitution.

    We deliberately avoid ``str.format_map`` here because Python format
    syntax treats ``:`` as a format-spec separator and ``!`` as a
    conversion flag — both are common in real-world column names
    (e.g. "营收:亿元", "比率(%)"). A simple replace also makes missing
    keys harmless: they keep their literal placeholder.
    """
    if not isinstance(item, dict) or not template:
        return template or ""
    out = template
    for k, v in item.items():
        if not isinstance(k, str):
            continue
        placeholder = "{" + k + "}"
        if placeholder in out:
            out = out.replace(placeholder, "" if v is None else str(v))
    return out


def _summarize_item(item: Dict[str, Any], source_type: str, max_len: int = 80) -> str:
    """One-line preview of an item, used in ``batch_item_start`` events."""
    if not isinstance(item, dict):
        text = str(item)
    elif source_type == "text_list":
        text = str(item.get("text") or item.get("content") or "")
    elif source_type == "word_files":
        text = str(item.get("file_name") or item.get("title") or "")
    elif source_type == "xlsx":
        parts = []
        for k, v in item.items():
            if k in ("row", "row_index"):
                continue
            parts.append(f"{k}={v}")
            if len(parts) >= 2:
                break
        text = " | ".join(parts) or f"row {item.get('row', '?')}"
    else:
        text = str(item)
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _load_plan(db, plan_id: str) -> Optional[BatchPlan]:
    return db.query(BatchPlan).filter(BatchPlan.plan_id == plan_id).first()


def _get_status(plan_id: str) -> Optional[str]:
    with SessionLocal() as db:
        plan = _load_plan(db, plan_id)
        return plan.status if plan else None


def _set_status(plan_id: str, status: str) -> None:
    with SessionLocal() as db:
        plan = _load_plan(db, plan_id)
        if plan:
            plan.status = status
            plan.updated_at = utc_now()
            db.commit()


def _read_results(plan_id: str) -> tuple[List[Dict[str, Any]], str, Dict[str, int]]:
    """Return (item_results, status, progress_counts)."""
    with SessionLocal() as db:
        plan = _load_plan(db, plan_id)
        if not plan:
            return [], "missing", {"done": 0, "success": 0, "failed": 0}
        progress = dict(plan.progress or {})
        results = list(progress.get("results") or [])
        counts = {
            "done": int(progress.get("done", 0)),
            "success": int(progress.get("success", 0)),
            "failed": int(progress.get("failed", 0)),
        }
        return results, plan.status, counts


def _blocking_item_run(plan_id: str, item_index: int) -> Optional[str]:
    """Return an unresolved inner run that forbids regenerating this batch item."""
    with SessionLocal() as db:
        rows = (
            db.query(ChatRun)
            .filter(ChatRun.status.in_(("pending", "running", "needs_attention")))
            .order_by(ChatRun.created_at.desc())
            .all()
        )
        for row in rows:
            payload = row.request_payload if isinstance(row.request_payload, dict) else {}
            if (
                payload.get("kind") == "batch_item"
                and payload.get("plan_id") == plan_id
                and str(payload.get("item_index", "")) == str(item_index)
            ):
                return str(row.run_id)
    return None


def _batch_item_attempt_run_id(
    user_id: str,
    plan_id: str,
    item_index: int,
    attempt: int,
) -> str:
    """Stable admission identity shared by every process for one item attempt."""
    raw = f"{user_id}:{plan_id}:{item_index}:{attempt}".encode("utf-8")
    return f"run_batch_{hashlib.sha256(raw).hexdigest()[:48]}"


def _append_result(plan_id: str, result: Dict[str, Any]) -> Dict[str, int]:
    """Atomically append a single item result to plan.progress.results
    and bump the success/failed/done counters. Returns updated counts."""
    with SessionLocal() as db:
        plan = _load_plan(db, plan_id)
        if not plan:
            return {"done": 0, "success": 0, "failed": 0}
        prog = dict(plan.progress or {})
        for count in ("done", "success", "failed"):
            prog.setdefault(count, 0)
        results: List[Dict[str, Any]] = list(prog.get("results") or [])
        results.append(result)
        prog["results"] = results
        prog["done"] = int(prog.get("done", 0)) + 1
        if result.get("status") == "success":
            prog["success"] = int(prog.get("success", 0)) + 1
        else:
            prog["failed"] = int(prog.get("failed", 0)) + 1
        plan.progress = prog
        # SQLAlchemy needs a hint that the JSONB column was mutated.
        flag_modified(plan, "progress")
        plan.updated_at = utc_now()
        db.commit()
        return {
            "done": prog["done"],
            "success": prog["success"],
            "failed": prog["failed"],
        }


# ---------------------------------------------------------------------------
# Background task registry
# ---------------------------------------------------------------------------


_active_tasks: Dict[str, asyncio.Task] = {}
_registry_lock: Optional[asyncio.Lock] = None


def _get_lock() -> asyncio.Lock:
    """Lazy-init the lock so this module is importable outside an event loop."""
    global _registry_lock
    if _registry_lock is None:
        _registry_lock = asyncio.Lock()
    return _registry_lock


async def _ensure_background_task(plan_id: str, user_id: str) -> None:
    """Start the background runner for *plan_id* if it isn't already running.

    The task survives client disconnect — it iterates plan.items, persists
    per-item results to DB, and exits when all items are processed (or the
    plan is cancelled). Subsequent /stream reconnects find the live task
    here and tail its output via DB polling.
    """
    async with _get_lock():
        existing = _active_tasks.get(plan_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(
            _run_until_done(plan_id, user_id),
            name=f"batch_runner:{plan_id}",
        )
        _active_tasks[plan_id] = task

        def _cleanup(t: asyncio.Task) -> None:
            # Remove from registry once finished. Log unexpected errors.
            _active_tasks.pop(plan_id, None)
            if t.cancelled():
                logger.info("[batch] runner task cancelled plan_id=%s", plan_id)
                return
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "[batch] runner task crashed plan_id=%s: %s",
                    plan_id,
                    exc,
                )

        task.add_done_callback(_cleanup)


def cancel_running_task(plan_id: str) -> bool:
    """Cancel the in-flight runner task for *plan_id* if one exists.

    Without this, /cancel only flags the DB and the runner keeps blocking
    on the current item's LLM call — the user sees no response until that
    item completes (often 30s+). Cancelling the asyncio task interrupts
    the current item immediately.

    The task's done_callback removes it from the registry; ``_run_until_done``
    won't write a final status because cancellation propagates as
    CancelledError, but that's fine — the /cancel endpoint already set
    plan.status = CANCELLED in the DB.

    Returns True if a task was found and cancellation was requested.
    """
    task = _active_tasks.get(plan_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


# ---------------------------------------------------------------------------
# Background runner — runs detached from the SSE handler
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SSE follower — survives reconnects by reading DB state
# ---------------------------------------------------------------------------


_POLL_INTERVAL_SEC = 0.4
# After this much silence, emit a batch_heartbeat event as keepalive, so nginx /
# intermediate reverse proxies / client proxies don't drop the idle connection
# during long LLM calls.
# The frontend batchStore ignores unknown event types, so no extra handling is needed.
_HEARTBEAT_INTERVAL_SEC = 15.0


class BatchOrchestrator:
    """Yield SSE-shaped events for a plan's execution.

    On every /stream call this:
      1. Verifies plan + ownership
      2. Ensures the background runner task is alive
      3. Emits known item_start/item_done pairs from plan.progress.results
      4. Polls DB for new results until the plan is done or cancelled
      5. Emits final batch_done

    Because reading is decoupled from the producer, refreshing the page or
    switching chats does NOT stop execution — it only disconnects the
    follower. Reconnecting replays everything.
    """

    def __init__(self, plan_id: str, user_id: str):
        self.plan_id = plan_id
        self.user_id = user_id

    async def run(self) -> AsyncIterator[Dict[str, Any]]:
        # ── Verify access + state ───────────────────────────────────────
        with SessionLocal() as db:
            plan = _load_plan(db, self.plan_id)
            if plan is None:
                yield {"type": "batch_error", "plan_id": self.plan_id, "error": "plan not found"}
                return
            if plan.user_id != self.user_id:
                yield {"type": "batch_error", "plan_id": self.plan_id, "error": "permission denied"}
                return
            if plan.status not in (CONFIRMED, RUNNING, DONE):
                yield {
                    "type": "batch_error",
                    "plan_id": self.plan_id,
                    "error": f"plan not runnable (status={plan.status})",
                }
                return
            total = len(plan.items or [])
            source_type = plan.source_type
            items_summary = [_summarize_item(it, source_type) for it in (plan.items or [])]

        # ── Kick off (or attach to) the background runner ──────────────
        # Skip the kick-off if the plan is already terminal so we just
        # replay history.
        if _get_status(self.plan_id) not in _TERMINAL:
            await _ensure_background_task(self.plan_id, self.user_id)

        # ── Replay + tail phase ────────────────────────────────────────
        emitted = 0
        last_yield_ts = asyncio.get_event_loop().time()
        while True:
            results, status, counts = _read_results(self.plan_id)

            # Emit any new (or initial-replay) results.
            while emitted < len(results):
                rec = results[emitted]
                idx = rec.get("index", emitted)
                summary = rec.get("item_summary") or (
                    items_summary[idx] if idx < len(items_summary) else f"item {idx}"
                )
                yield {
                    "type": "batch_item_start",
                    "plan_id": self.plan_id,
                    "index": idx,
                    "total": total,
                    "item_summary": summary,
                }
                done_event = {
                    "type": "batch_item_done",
                    "plan_id": self.plan_id,
                    "index": idx,
                    "total": total,
                    "status": rec.get("status", "success"),
                    "retry_count": rec.get("retry_count", 0),
                    "progress": counts,
                    # Pass through everything the chat bubble can render so
                    # the frontend uses its existing primitives (markdown,
                    # tool cards, file/artifact cards, citation chips).
                    "tool_calls": rec.get("tool_calls") or [],
                    "artifacts": rec.get("artifacts") or [],
                    "citations": rec.get("citations") or [],
                }
                if rec.get("status") == "success":
                    done_event["content"] = rec.get("content", "")
                else:
                    done_event["error"] = rec.get("error", "")
                yield done_event
                emitted += 1
                last_yield_ts = asyncio.get_event_loop().time()

            # Termination?
            if status in _TERMINAL:
                with SessionLocal() as db:
                    plan = _load_plan(db, self.plan_id)
                    environment_error = (
                        (plan.progress or {}).get("environment_error") if plan else None
                    )
                if environment_error:
                    yield {
                        "type": "batch_error",
                        "plan_id": self.plan_id,
                        "error": environment_error,
                    }
                yield {
                    "type": "batch_done",
                    "plan_id": self.plan_id,
                    "status": status,
                    "total": total,
                    "success": counts.get("success", 0),
                    "failed": counts.get("failed", 0),
                }
                return

            # Still running — keep tailing DB until something changes.
            # If silent for too long, yield a heartbeat to push SSE keepalive bytes to the
            # client, so intermediate reverse proxies don't kill the connection as idle
            # during long LLM calls.
            now = asyncio.get_event_loop().time()
            if now - last_yield_ts >= _HEARTBEAT_INTERVAL_SEC:
                yield {
                    "type": "batch_heartbeat",
                    "plan_id": self.plan_id,
                    "ts": int(now),
                }
                last_yield_ts = now
            await asyncio.sleep(_POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# TTL housekeeping (called by a startup task or cron — best effort)
# ---------------------------------------------------------------------------


def cleanup_expired_plans() -> int:
    """Delete plans whose expires_at has passed. Returns count deleted."""
    now = utc_now()
    with SessionLocal() as db:
        expired = (
            db.query(BatchPlan)
            .filter(BatchPlan.expires_at.isnot(None))
            .filter(BatchPlan.expires_at < now)
            .all()
        )
        n = len(expired)
        for p in expired:
            db.delete(p)
        db.commit()
        return n


__all__ = ["BatchOrchestrator", "cleanup_expired_plans"]
