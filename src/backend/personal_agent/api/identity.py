"""Personal Agent identity routes."""

from fastapi import APIRouter, Depends

from core.infra.responses import success_response

from personal_agent.api.schemas import Settings
from personal_agent.api.schemas import Initialize
from personal_agent.api.dependencies import service
from personal_agent.api.dependencies import perform

router = APIRouter()


@router.get("")
def get_agent(svc=Depends(service)):
    return success_response(data=svc.identity.view())


@router.post("/initialize")
def initialize(body: Initialize | None = None, svc=Depends(service)):
    from personal_agent.conversation.front import hatch

    def run():
        view = svc.identity.ensure()
        # Record the browser timezone once. A later explicit setting wins.
        if body and body.timezone and not view["config"].get("timezone"):
            try:
                view = svc.identity.configure({"timezone": body.timezone})
            except ValueError:
                pass
        # A new agent wakes up and greets the user first.
        hatch(svc)
        return view

    return perform(run)


@router.get("/capabilities")
def capability_options(svc=Depends(service)):
    return perform(
        lambda: {k: v for k, v in svc.identity.capability_options().items() if k != "blocked_ids"}
    )


@router.patch("/settings")
def configure(body: Settings, svc=Depends(service)):
    patch = body.model_dump(exclude_none=True)
    # Only the fields the client sent: a null channel_id clears the IM target.
    if body.heartbeat is not None:
        patch["heartbeat"] = body.heartbeat.model_dump(exclude_unset=True)
    return perform(lambda: svc.identity.configure(patch))


@router.post("/pause")
def pause(svc=Depends(service)):
    svc.identity.ensure()
    row = svc.identity.agent()
    row.status = "paused"
    svc.db.commit()
    return success_response(data=svc.identity.view(row))


@router.post("/resume")
def resume(svc=Depends(service)):
    svc.identity.ensure()
    row = svc.identity.agent()
    row.status = "ready"
    svc.db.commit()
    return success_response(data=svc.identity.view(row))
