"""Personal Agent channels routes."""

from fastapi import APIRouter, Depends

from personal_agent.api.schemas import ChannelAccount, ChannelRoute
from personal_agent.api.dependencies import service
from personal_agent.api.dependencies import perform

router = APIRouter()


@router.get("/channels")
def channel_bots(svc=Depends(service)):
    from personal_agent.channels.channel_bridge import bots

    return perform(lambda: bots(svc.db, svc.user_id))


@router.put("/channels/{channel_id}")
def route_channel(channel_id: str, body: ChannelRoute, svc=Depends(service)):
    from personal_agent.channels.channel_bridge import route

    svc.identity.ensure()
    return perform(lambda: route(svc.db, svc.user_id, channel_id, body.enabled))


@router.post("/channels/{channel_id}/allowed")
def allow_channel_account(channel_id: str, body: ChannelAccount, svc=Depends(service)):
    from personal_agent.channels.channel_bridge import allow_account

    svc.identity.ensure()
    return perform(lambda: allow_account(svc.db, svc.user_id, channel_id, key=body.key, sender_id=body.sender_id))


@router.delete("/channels/{channel_id}/allowed")
def remove_channel_account(channel_id: str, key: str, svc=Depends(service)):
    from personal_agent.channels.channel_bridge import remove_account

    svc.identity.ensure()
    return perform(lambda: remove_account(svc.db, svc.user_id, channel_id, key))
