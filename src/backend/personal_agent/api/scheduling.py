"""Personal Agent scheduling routes."""

from fastapi import APIRouter, Depends

from personal_agent.api.schemas import SchedulePatch
from personal_agent.api.dependencies import service
from personal_agent.api.dependencies import perform

router = APIRouter()


@router.post("/heartbeat/run")
def run_heartbeat(svc=Depends(service)):
    from personal_agent.scheduling.heartbeat import run_now

    def run():
        svc.identity.ensure()
        return run_now(svc.db, svc.user_id)

    return perform(run)


@router.get("/dreaming")
def dreaming_status(svc=Depends(service)):
    from personal_agent.workspace.dreaming import view

    return perform(lambda: view(svc.db, svc.identity.agent()))


@router.post("/dreaming/run")
def run_dreaming(svc=Depends(service)):
    from personal_agent.workspace.dreaming import run_now

    def run():
        svc.identity.ensure()
        return run_now(svc.db, svc.user_id)

    return perform(run)


@router.patch("/schedules/{ident}")
def update_schedule(ident: str, body: SchedulePatch, svc=Depends(service)):
    from personal_agent.scheduling.schedules import update_schedule as change

    return perform(lambda: change(svc.db, svc.user_id, ident, body.model_dump(exclude_unset=True)))


@router.delete("/schedules/{ident}")
def delete_schedule(ident: str, svc=Depends(service)):
    from personal_agent.scheduling.schedules import delete

    return perform(lambda: delete(svc.db, svc.user_id, ident))
