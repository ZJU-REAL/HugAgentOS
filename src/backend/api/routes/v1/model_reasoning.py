"""Administrator-only, non-persisting reasoning effort discovery."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_system_settings
from core.db.threads import run_in_session
from core.models.repository import get_provider
from core.infra.responses import success_response
from core.models.providers.reasoning_probe import discover_reasoning_efforts

router = APIRouter()


class DetectReasoningRequest(BaseModel):
    provider: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model_name: str
    api_protocol: str | None = None
    provider_id: str | None = None


@router.post("/providers/detect-reasoning", summary="探测模型支持的思考档位")
async def detect_reasoning_levels(
    body: DetectReasoningRequest,
    _: None = Depends(require_system_settings),
):
    api_key = body.api_key
    if body.provider_id and not api_key:
        existing = await run_in_session(get_provider, body.provider_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Provider not found")
        api_key = existing.api_key or ""
    result = await discover_reasoning_efforts(
        provider=body.provider,
        base_url=body.base_url,
        api_key=api_key,
        model_name=body.model_name,
        api_protocol=body.api_protocol,
    )
    return success_response(data=result)
