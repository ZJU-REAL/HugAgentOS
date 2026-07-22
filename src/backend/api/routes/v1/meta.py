"""Public build-edition probe."""

from api.middleware.edition import edition_probe_payload
from core.infra.responses import success_response
from fastapi import APIRouter

router = APIRouter(prefix="/v1/meta", tags=["meta"])


@router.get("/edition", summary="当前部署的版本与能力位")
async def get_edition():
    return success_response(data=edition_probe_payload())
