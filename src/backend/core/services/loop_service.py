"""Autonomous Loop service layer — CRUD + audit persistence.

See internal design docs (§2.2 components, §3 data model).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.db.models import AgentLoop, LoopIteration
from core.infra.logging import get_logger

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LoopService:
    def __init__(self, db: Session):
        self.db = db

    # ── CRUD ────────────────────────────────────────────────────────────────
    def create_loop(
        self,
        *,
        user_id: str,
        title: str,
        goal_spec: Dict[str, Any],
        budget: Dict[str, Any],
        chat_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> AgentLoop:
        # project_id: the project the user selected in the input box — the loop is fully
        # bound to it; the worker operates directly in that project folder (where the site
        # source lives) and publishing goes through publish_site. Stored in metadata so a
        # resumed run can read it back too.
        meta: Dict[str, Any] = {}
        if project_id:
            meta["project_id"] = project_id
        loop = AgentLoop(
            loop_id=f"loop_{uuid.uuid4().hex[:16]}",
            user_id=user_id,
            chat_id=chat_id,
            title=title or (goal_spec.get("objective", "")[:80]),
            goal_spec=goal_spec,
            budget=budget,
            status="created",
            extra_data=(meta or None),
        )
        self.db.add(loop)
        self.db.commit()
        self.db.refresh(loop)
        return loop

    def get_loop(self, loop_id: str, *, user_id: Optional[str] = None) -> Optional[AgentLoop]:
        q = self.db.query(AgentLoop).filter(AgentLoop.loop_id == loop_id)
        if user_id is not None:
            q = q.filter(AgentLoop.user_id == user_id)
        return q.first()

    def list_loops(self, user_id: str, *, limit: int = 50) -> List[AgentLoop]:
        return (
            self.db.query(AgentLoop)
            .filter(AgentLoop.user_id == user_id)
            .order_by(AgentLoop.created_at.desc())
            .limit(limit)
            .all()
        )

    def list_iterations(self, loop_id: str) -> List[LoopIteration]:
        return (
            self.db.query(LoopIteration)
            .filter(LoopIteration.loop_id == loop_id)
            .order_by(LoopIteration.seq)
            .all()
        )

    # ── State transitions / audit ───────────────────────────────────────────────
    def mark_running(self, loop_id: str, *, workspace_session: Optional[str] = None) -> None:
        loop = self.get_loop(loop_id)
        if not loop:
            return
        loop.status = "running"
        if workspace_session:
            loop.workspace_session = workspace_session
        loop.updated_at = _now()
        self.db.commit()

    # ── Requirement ledger DB mirror (reliable source of truth for resumable runs, not reliant on sandbox snapshots) ──
    def save_ledger(self, loop_id: str, ledger: Dict[str, Any]) -> None:
        """Mirror the driver's requirement ledger (feature_list.json) into agent_loops.metadata.

        The sandbox /workspace is only a working cache — rebuild/restart/machine-switch/an
        unsaved snapshot all lose it. Once the ledger is persisted to the DB, a resumed run
        recovers the requirement list + progress + frozen verification scripts from here
        first, rather than re-decomposing the goal from scratch.
        """
        loop = self.get_loop(loop_id)
        if not loop:
            return
        meta = dict(loop.extra_data or {})
        meta["ledger"] = ledger
        loop.extra_data = meta
        # iteration_count follows the ledger so list/detail pages reflect progress immediately (no need to wait for the terminal state).
        try:
            loop.iteration_count = int(ledger.get("iteration", loop.iteration_count) or 0)
        except (TypeError, ValueError):
            pass
        loop.updated_at = _now()
        self.db.commit()

    def load_ledger(self, loop_id: str) -> Optional[Dict[str, Any]]:
        """Read back the DB-mirrored requirement ledger; return None if absent (falls back to sandbox ledger / first-launch initialization)."""
        loop = self.get_loop(loop_id)
        if not loop:
            return None
        meta = loop.extra_data or {}
        ledger = meta.get("ledger")
        if isinstance(ledger, dict) and ledger.get("requirements"):
            return ledger
        return None

    def persist_result(self, loop_id: str, result: Any) -> None:
        """Persist the result of one run_autonomous_loop: loop terminal state + each round's LoopIteration.

        ``result`` is an orchestration.autonomous_loop.LoopResult.
        """
        loop = self.get_loop(loop_id)
        if not loop:
            logger.warning("persist_result: loop %s not found", loop_id)
            return
        loop.status = result.status
        loop.iteration_count = result.iterations
        loop.tokens_spent = int(result.tokens_spent)
        loop.final_score = result.final_score
        loop.result_summary = (result.reason or "")[:2000]
        loop.updated_at = _now()

        # Overwrite-rewrite the audit trail (idempotent)
        self.db.query(LoopIteration).filter(LoopIteration.loop_id == loop_id).delete()
        for rec in result.history:
            self.db.add(
                LoopIteration(
                    iteration_id=f"it_{uuid.uuid4().hex[:16]}",
                    loop_id=loop_id,
                    seq=rec.get("seq", 0),
                    verdict=rec.get("verdict"),
                    score=rec.get("score"),
                    reasoning=(rec.get("reason") or "")[:4000],
                    tool_calls=rec.get("tool_calls", 0),
                    tokens=rec.get("tokens", 0),
                    decided_by=rec.get("decided_by"),
                )
            )
        self.db.commit()
