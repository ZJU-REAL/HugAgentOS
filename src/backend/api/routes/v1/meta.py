"""Edition / license probe (shared by CE/EE, no auth—— only exposes the edition, mode, and feature-flag boolean map).

An unauthenticated endpoint; it must never return license details
(license_id/customer name/seats/expiry date)—— those fields are only exposed by the
CONFIG_TOKEN-authenticated /v1/config/license. ``mode`` is reserved for scenarios
like the login page hinting "license has expired".
"""

from fastapi import APIRouter

from core.config.settings import settings
from core.infra.responses import success_response
from core.licensing import license_manager

router = APIRouter(prefix="/v1/meta", tags=["meta"])


@router.get("/edition", summary="当前部署的版本与能力位")
async def get_edition():
    return success_response(
        data={
            "edition": settings.edition.edition,
            "mode": license_manager.mode(),
            "features": license_manager.features_map(),
        }
    )
