"""Personal Agent tasks routes."""

from fastapi import APIRouter, Depends, Query

from core.infra.responses import success_response

from personal_agent.api.schemas import Approval
from personal_agent.api.schemas import Reconciliation
from personal_agent.api.dependencies import service
from personal_agent.api.dependencies import perform

router = APIRouter()


@router.get("/work-items")
def tasks(svc=Depends(service)):
    return success_response(data=svc.tasks.list())


@router.post("/work-items/{ident}/cancel")
def cancel(ident: str, svc=Depends(service)):
    return perform(lambda: svc.tasks.cancel(ident))


@router.post("/work-items/{ident}/approve")
def task_approval(ident: str, body: Approval, svc=Depends(service)):
    from personal_agent.tasks.approvals import approve

    return perform(lambda: approve(svc, ident, body.approval_id, body.accept, body.scope))


@router.post("/work-items/{ident}/retry")
def retry_task(ident: str, svc=Depends(service)):
    return perform(lambda: svc.tasks.retry(ident))


@router.post("/work-items/{ident}/reconcile")
def reconcile_task(ident: str, body: Reconciliation, svc=Depends(service)):
    from personal_agent.tasks.approvals import reconcile

    return perform(lambda: reconcile(svc, ident, body.action_id, body.outcome))


@router.get("/work-items/{ident}/conversation")
def task_conversation(ident: str, before_seq: int | None = Query(None, ge=1), svc=Depends(service)):
    from personal_agent.tasks.task_threads import history

    return perform(lambda: history(svc, ident, before_seq))


@router.get("/outputs")
def outputs(before: str | None = None, limit: int = Query(50, ge=1, le=100), svc=Depends(service)):
    from personal_agent.delivery.outputs import listing

    return perform(lambda: listing(svc, before, limit))
