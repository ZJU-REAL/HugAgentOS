"""Authenticated desktop upload and administrator-only management copies."""

import json
from typing import Literal

from api.deps import require_config
from api.routes.v1.desktop_capability import _require_capability_user
from core.db.engine import SessionLocal, get_db
from core.db.models import UserShadow
from core.db.models.observability import DesktopSyncDevice
from core.infra.responses import paginated_response, success_response
from core.services.desktop_observability import MAX_BATCH_BYTES, ingest, list_records
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/v1/desktop/observability", tags=["Desktop Observability"])
admin_router = APIRouter(
    prefix="/v1/admin/desktop-observability",
    tags=["Desktop Observability"],
    dependencies=[Depends(require_config)],
)


@router.post("/batch")
async def receive_batch(request: Request, user_id: str = Depends(_require_capability_user)):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BATCH_BYTES:
            raise HTTPException(413, "observation batch too large")
    try:
        body = json.loads(data)
        if not isinstance(body, dict):
            raise ValueError()
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(400, "invalid observation batch") from None
    device = request.headers.get("x-desktop-device-id", "")

    def write():
        with SessionLocal() as db:
            return ingest(db, user_id, device, body)

    try:
        return success_response(data=await run_in_threadpool(write))
    except (ValueError, TypeError):
        raise HTTPException(400, "invalid observation batch") from None


@admin_router.get("/records")
def records(
    kind: (
        Literal["session", "message", "run", "tool", "skill", "agent", "usage", "model"] | None
    ) = None,
    user_id: str | None = Query(None, max_length=64),
    device_id: str | None = Query(None, max_length=128),
    chat_id: str | None = Query(None, max_length=128),
    run_id: str | None = Query(None, max_length=128),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db=Depends(get_db),
):
    rows, total = list_records(
        db,
        kind=kind,
        user_id=user_id,
        device_id=device_id,
        chat_id=chat_id,
        run_id=run_id,
        page=page,
        page_size=page_size,
    )
    user_ids = {row["user_id"] for row in rows}
    names = (
        dict(
            db.execute(
                select(UserShadow.user_id, UserShadow.username).where(
                    UserShadow.user_id.in_(user_ids)
                )
            ).all()
        )
        if user_ids
        else {}
    )
    for row in rows:
        row["username"] = names.get(row["user_id"], row["user_id"])
    return paginated_response(items=rows, page=page, page_size=page_size, total_items=total)


@admin_router.get("/devices")
def devices(db=Depends(get_db)):
    rows = db.scalars(
        select(DesktopSyncDevice).order_by(DesktopSyncDevice.last_seen.desc()).limit(1000)
    ).all()
    return success_response(
        data=[
            {c.name: getattr(r, c.name) for c in DesktopSyncDevice.__table__.columns} for r in rows
        ]
    )


@admin_router.get("/health")
def health():
    from core.services.desktop_gateway_observability import health_snapshot

    return success_response(data=health_snapshot())
