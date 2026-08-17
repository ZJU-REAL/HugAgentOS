"""作业编排运行时（Job Runtime）的持久化服务层。

台账（``job_items``）是完成判据的唯一真源：驱动侧读它决定收敛，脚本侧读它做断点续跑。
所有写入按 ``(job_id, item_key)`` 幂等——脚本重跑同一份 seed 不会重复建项、也不会把
已完成项打回 pending。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.db.models import Job, JobCall, JobItem

logger = logging.getLogger(__name__)

# 台账状态：done/not_found 视为"这一项不必再做"，其余都还欠着
SETTLED_STATUSES = ("done", "not_found")

DEFAULT_BUDGET: Dict[str, int] = {
    "max_calls": 5000,
    "max_tokens": 20_000_000,
    "max_seconds": 7200,
    "concurrency": 8,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:16]}"


def mint_token() -> str:
    """一次性 job token：随 job 建立、随 job 终态失效。

    只用于回调三端点（agent / ledger / log），换不到模型凭据、碰不到其它 job。
    """
    return secrets.token_urlsafe(32)


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a or ""), str(b or ""))


class JobService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ── 生命周期 ──────────────────────────────────────────────────────

    def create(
        self,
        *,
        user_id: str,
        chat_id: Optional[str],
        name: str,
        script_path: str,
        script_text: str,
        sandbox_session_id: Optional[str],
        budget: Optional[Dict[str, Any]] = None,
        start_params: Optional[Dict[str, Any]] = None,
    ) -> Job:
        merged = dict(DEFAULT_BUDGET)
        for k, v in (budget or {}).items():
            if isinstance(v, (int, float)) and v > 0:
                merged[k] = int(v)
        # 并发上限同时受全局护栏约束，防止单会话吃光全后端的子作业槽位
        merged["concurrency"] = max(1, min(int(merged["concurrency"]), 16))

        job = Job(
            job_id=new_job_id(),
            user_id=user_id,
            chat_id=chat_id or None,
            name=(name or "")[:255],
            status="pending",
            script_path=script_path,
            script_text=script_text or "",
            sandbox_session_id=sandbox_session_id,
            budget=merged,
            usage={"calls": 0, "tokens": 0, "seconds": 0},
            extra_data={"token": mint_token(), "start_params": start_params or {}},
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self.db.query(Job).filter(Job.job_id == job_id).first()

    def verify_token(self, job_id: str, token: str) -> Optional[Job]:
        """回调鉴权：token 匹配且 job 未进终态才放行。"""
        job = self.get(job_id)
        if job is None:
            return None
        if job.status in ("completed", "failed", "cancelled"):
            return None
        stored = str((job.extra_data or {}).get("token") or "")
        if not stored or not _same(stored, token):
            return None
        return job

    def mark_running(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None or job.status not in ("pending", "interrupted", "paused"):
            return
        job.status = "running"
        job.started_at = job.started_at or _utcnow()
        self.db.commit()

    def finish(self, job_id: str, status: str, *, error: Optional[str] = None) -> None:
        job = self.get(job_id)
        if job is None or job.status in ("completed", "failed", "cancelled"):
            return
        job.status = status
        job.error_message = (error or "")[:4000] or None
        job.completed_at = _utcnow()
        # 终态即销毁 token：沙箱可能被别的会话复用，旧脚本不得再回调
        meta = dict(job.extra_data or {})
        meta.pop("token", None)
        job.extra_data = meta
        flag_modified(job, "extra_data")
        self.db.commit()

    def rotate_token(self, job_id: str) -> Optional[str]:
        """resume 时换发新 token（旧 token 已在 finish 时销毁）。"""
        job = self.get(job_id)
        if job is None:
            return None
        token = mint_token()
        meta = dict(job.extra_data or {})
        meta["token"] = token
        job.extra_data = meta
        flag_modified(job, "extra_data")
        self.db.commit()
        return token

    # ── 台账 ─────────────────────────────────────────────────────────

    def seed(self, job_id: str, items: List[Dict[str, Any]]) -> Dict[str, int]:
        """幂等建项：已存在的 item_key 一律跳过（不覆盖已有状态与结果）。"""
        if not items:
            return {"created": 0, "skipped": 0}
        existing = {
            row.item_key
            for row in self.db.query(JobItem.item_key).filter(JobItem.job_id == job_id).all()
        }
        created = 0
        for it in items:
            key = str(it.get("key") or "").strip()[:128]
            if not key or key in existing:
                continue
            self.db.add(
                JobItem(
                    job_id=job_id,
                    item_key=key,
                    status="pending",
                    payload=it.get("payload") or {},
                    attempts=0,
                )
            )
            existing.add(key)
            created += 1
        self.db.commit()
        return {"created": created, "skipped": len(items) - created}

    def pending(
        self, job_id: str, *, status: str = "pending", limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        q = (
            self.db.query(JobItem)
            .filter(JobItem.job_id == job_id, JobItem.status == status)
            .order_by(JobItem.item_key)
        )
        if limit:
            q = q.limit(int(limit))
        return [
            {
                "key": r.item_key,
                "payload": r.payload or {},
                "status": r.status,
                "attempts": r.attempts or 0,
                "result": r.result,
                "review": r.review,
            }
            for r in q.all()
        ]

    def update_item(
        self,
        job_id: str,
        item_key: str,
        *,
        status: Optional[str] = None,
        result: Optional[Any] = None,
        review: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        bump_attempts: bool = False,
    ) -> bool:
        row = (
            self.db.query(JobItem)
            .filter(JobItem.job_id == job_id, JobItem.item_key == str(item_key)[:128])
            .first()
        )
        if row is None:
            return False
        if status:
            row.status = status
        if result is not None:
            row.result = result
        if review is not None:
            merged = dict(row.review or {})
            merged.update(review)
            row.review = merged
        if error is not None:
            row.error = str(error)[:4000]
        if bump_attempts:
            row.attempts = (row.attempts or 0) + 1
        row.updated_at = _utcnow()
        self.db.commit()
        return True

    def stats(self, job_id: str) -> Dict[str, int]:
        rows = (
            self.db.query(JobItem.status, func.count())
            .filter(JobItem.job_id == job_id)
            .group_by(JobItem.status)
            .all()
        )
        out: Dict[str, int] = {s: int(c) for s, c in rows}
        total = sum(out.values())
        settled = sum(out.get(s, 0) for s in SETTLED_STATUSES)
        return {
            "total": total,
            "done": out.get("done", 0),
            "pending": out.get("pending", 0),
            "failed": out.get("failed", 0),
            "not_found": out.get("not_found", 0),
            "needs_review": out.get("needs_review", 0),
            "running": out.get("running", 0),
            "settled": settled,
            "remaining": total - settled,
        }

    # ── 用量与审计 ────────────────────────────────────────────────────

    def budget_left(self, job_id: str) -> Dict[str, int]:
        job = self.get(job_id)
        if job is None:
            return {"calls_left": 0, "tokens_left": 0, "seconds_left": 0}
        budget = dict(DEFAULT_BUDGET)
        budget.update(job.budget or {})
        usage = job.usage or {}
        started = job.started_at or job.created_at or _utcnow()
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = int((_utcnow() - started).total_seconds())
        return {
            "calls_left": max(0, int(budget["max_calls"]) - int(usage.get("calls", 0))),
            "tokens_left": max(0, int(budget["max_tokens"]) - int(usage.get("tokens", 0))),
            "seconds_left": max(0, int(budget["max_seconds"]) - elapsed),
            "concurrency": int(budget.get("concurrency", 8)),
        }

    def add_usage(self, job_id: str, *, calls: int = 0, tokens: int = 0) -> None:
        job = self.get(job_id)
        if job is None:
            return
        usage = dict(job.usage or {})
        usage["calls"] = int(usage.get("calls", 0)) + int(calls)
        usage["tokens"] = int(usage.get("tokens", 0)) + int(tokens)
        job.usage = usage
        flag_modified(job, "usage")
        self.db.commit()

    def record_call(
        self,
        job_id: str,
        *,
        item_key: Optional[str],
        prompt: str,
        model: Optional[str],
        duration_ms: int,
        status: str,
        tokens: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        seq = (
            self.db.query(func.count(JobCall.call_id)).filter(JobCall.job_id == job_id).scalar()
            or 0
        )
        self.db.add(
            JobCall(
                call_id=f"jc_{uuid.uuid4().hex[:16]}",
                job_id=job_id,
                item_key=(item_key or None),
                seq=int(seq) + 1,
                prompt_hash=hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:64],
                model=(model or "")[:128] or None,
                tokens=tokens or {},
                duration_ms=int(duration_ms),
                status=status,
                error=(error or "")[:4000] or None,
            )
        )
        self.db.commit()
