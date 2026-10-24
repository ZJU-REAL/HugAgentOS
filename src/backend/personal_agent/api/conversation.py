"""Personal Agent conversation routes."""

from fastapi import APIRouter, Depends, HTTPException, Query

from core.infra.responses import success_response

from personal_agent.api.schemas import Message
from personal_agent.api.dependencies import service
from personal_agent.api.dependencies import perform

router = APIRouter()


@router.post("/messages", status_code=202)
def submit(body: Message, svc=Depends(service)):
    from personal_agent.conversation.message_routing import admit

    ids = list(
        dict.fromkeys([*body.work_item_ids, *([body.work_item_id] if body.work_item_id else [])])
    )
    intent = "update" if body.work_item_id and body.intent == "auto" else body.intent

    def run():
        from personal_agent.conversation.conversations import latest_origin

        svc.identity.ensure()
        origin = (
            latest_origin(svc.db, svc.identity.agent(), svc.conversation.id())
            if svc.chat_id
            else None
        )
        extra = (
            {
                "origin": "channel",
                "channel_origin": {
                    key: value for key, value in origin.items() if key != "placeholder_id"
                },
            }
            if origin
            else None
        )
        return admit(
            svc,
            body.content,
            body.request_key,
            body.artifact_ids,
            ids,
            intent,
            body.reply_to_message_id,
            extra=extra,
        )

    return perform(run)


@router.get("/sidebar-status")
def sidebar_status(after_seq: int = Query(0, ge=0), svc=Depends(service)):
    from personal_agent.conversation.sidebar_status import sidebar_status as snapshot

    return success_response(data=snapshot(svc, after_seq))


@router.get("/messages")
def messages(
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    before_seq: int | None = Query(None, ge=1),
    svc=Depends(service),
):
    return perform(lambda: svc.conversation.list(after_seq, limit, before_seq))


@router.get("/conversations")
def channel_conversations(svc=Depends(service)):
    from personal_agent.conversation.conversations import listing

    return perform(lambda: listing(svc))


@router.get("/monitor")
async def monitor(svc=Depends(service)):
    import asyncio
    from personal_agent.workspace.file_memory import status as memory_status
    from personal_agent.conversation.message_routing import pending
    from personal_agent.delivery.outputs import listing
    from personal_agent.computer.devices import list_devices
    from integrations.email.models import EmailConnection
    from personal_agent.scheduling.schedules import listing as schedule_listing
    from personal_agent.scheduling.heartbeat import view as heartbeat_view

    def snapshot():
        # Polled every few seconds: keep the database reads off the event loop.
        agent = svc.identity.view()
        if svc.chat_id:
            svc.conversation.id()
        email = svc.db.get(EmailConnection, svc.user_id)
        return {
            "file_memory": memory_status(svc.db, svc.identity.agent()),
            "agent": agent,
            "tasks": svc.tasks.list(),
            "outputs": listing(svc, limit=12),
            "devices": list_devices(svc.user_id),
            "turns": pending(svc),
            "schedules": schedule_listing(agent["config"]) if agent else [],
            "heartbeat": heartbeat_view(agent["config"]) if agent else None,
            "email_connected": bool(email and email.status == "connected"),
        }

    return await monitor_result(snapshot)


async def monitor_result(snapshot):
    import asyncio

    try:
        return success_response(data=await asyncio.to_thread(snapshot))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
