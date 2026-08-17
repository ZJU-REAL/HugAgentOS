"""批量作业的**用户可见**只读视图 —— 喂输入框上方那条作业状态条。

与 ``internal_jobs.py`` 划清界限：那套端点是给沙箱里的作业脚本用的（job token 鉴权，
可写台账、可派子作业）；这里是给**人**看的（登录用户鉴权，只读、只给聚合数），
两者永远不共用鉴权，脚本拿到的 token 也永远换不到这里的数据。

为什么必须有它：工作流模式下作业跑在后台，主对话是安静的——不给一个零成本的进度视图，
用户根本无法判断"到底有没有在跑"，只能干等或反复追问（而每次追问都是一轮真实推理）。
状态条走这个接口轮询，代价是一次 SQL 聚合，和模型完全无关。
"""

from typing import List, Optional

from core.auth.backend import UserContext, get_current_user
from core.db.engine import SessionLocal
from core.db.models import Job
from core.infra.logging import get_logger
from core.infra.responses import success_response
from core.services.job_service import JobService
from fastapi import APIRouter, Depends, HTTPException, Query

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/jobs", tags=["Jobs"])

_LIVE_STATUSES = ("pending", "running")


def _view(svc: JobService, job: Job) -> dict:
    """一条作业的对外形状 —— 只给聚合，逐项明细一律不出这个接口。

    明细走 ``run_job(action='export')`` 落沙箱文件：几百上千项读进浏览器（或对话）
    毫无意义，还会把上下文撑爆。
    """
    stats = svc.stats(job.job_id)
    return {
        "job_id": job.job_id,
        "chat_id": job.chat_id,
        "name": job.name or "",
        "status": job.status,
        "stats": stats,
        "usage": dict(job.usage or {}),
        "budget_left": svc.budget_left(job.job_id),
        "error": job.error_message or "",
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


@router.get("", summary="列出作业（按会话过滤，供状态条轮询）")
async def list_jobs(
    chat_id: Optional[str] = Query(None, description="只看这个会话的作业"),
    live: bool = Query(True, description="true 只返回未结束的作业（状态条默认口径）"),
    limit: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
):
    """列出当前用户的作业。默认只给未结束的——状态条只关心"现在有没有在跑"。"""
    with SessionLocal() as db:
        svc = JobService(db)
        q = db.query(Job).filter(Job.user_id == user.user_id)
        if chat_id:
            q = q.filter(Job.chat_id == chat_id)
        if live:
            q = q.filter(Job.status.in_(_LIVE_STATUSES))
        rows: List[Job] = q.order_by(Job.created_at.desc()).limit(limit).all()
        return success_response(data={"jobs": [_view(svc, r) for r in rows]})


@router.get("/{job_id}", summary="查单个作业进度")
async def get_job(job_id: str, user: UserContext = Depends(get_current_user)):
    with SessionLocal() as db:
        svc = JobService(db)
        job = svc.get(job_id)
        if job is None or job.user_id != user.user_id:
            raise HTTPException(status_code=404, detail="job not found")
        return success_response(data=_view(svc, job))


@router.post("/{job_id}/cancel", summary="用户手动取消作业")
async def cancel_job(job_id: str, user: UserContext = Depends(get_current_user)):
    """状态条上的取消按钮 —— 跑歪了的作业不该只能等它烧完预算。"""
    from orchestration import job_runtime

    ok = await job_runtime.cancel_job(job_id, user_id=user.user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found or not cancellable")
    return success_response(data={"job_id": job_id, "status": "cancelled"})
