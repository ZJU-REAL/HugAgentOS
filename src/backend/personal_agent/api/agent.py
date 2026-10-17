"""Personal Agent HTTP router composition. Public URL paths stay stable."""

from fastapi import APIRouter
from personal_agent.api import (
    identity,
    conversation,
    tasks,
    scheduling,
    computer,
    channels,
    workspace,
)

router = APIRouter(tags=["personal-agent"])
for module in (identity, conversation, tasks, scheduling, computer, channels, workspace):
    router.include_router(module.router, prefix="/v1/personal-agent")
