"""Callback fencing: authentication and writes share one locked transaction."""

from contextlib import contextmanager

from core.db.engine import SessionLocal
from core.services.job_service import JobService
from fastapi import HTTPException


@contextmanager
def callback_session(job_id, token):
    with SessionLocal() as db:
        svc = JobService(db, commit=False)
        if not token or svc.verify_token(job_id, token, lock=True) is None:
            raise HTTPException(403, "job token 无效或作业已结束")
        yield svc
        db.commit()
