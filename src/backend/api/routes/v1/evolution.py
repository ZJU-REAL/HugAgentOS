"""Per-turn evolution settlement API (GCE ticket 06).

Deliberately tiny and read-only.  The SSE push is the fast path for a live
client; this is the durable one — a client that reloaded, or that disconnected
before settlement finished, asks here instead.

There is no write endpoint. Nothing about the evolution loop can be advanced
from the chat surface; all approval and release actions live behind the
enterprise console so that a chat client can never promote anything.
"""

from typing import Optional

from core.auth.backend import UserContext, get_current_user
from core.evolution.settlement import SETTLE_PENDING
from core.infra.exceptions import BadRequestError
from core.evolution.settlement_store import load_summary
from core.infra.responses import success_response
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/evolution", tags=["Evolution"])


@router.get("/turns/{message_id}", summary="查询某轮对话的进化结算结果")
async def get_turn_settlement(
    message_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Return the settlement summary for one turn.

    A turn still settling reports ``pending`` rather than 404 — the client is
    polling for an outcome that legitimately does not exist yet, and a 404 would
    read as "this turn is gone".
    """
    summary = load_summary(message_id)
    if summary is None:
        return success_response(
            data={"message_id": message_id, "state": SETTLE_PENDING, "mechanisms": []}
        )
    return success_response(data=summary)


class ContributionSettingRequest(BaseModel):
    enabled: bool = Field(..., description="是否允许本账号的对话作为进化证据")


@router.get("/settings", summary="获取本账号的进化参与设置")
async def get_evolution_settings(user: UserContext = Depends(get_current_user)):
    """Legacy view of the single evolution switch.

    The separate contribution toggle is gone from the product; participation now
    simply follows ``evolution_prefs.enabled``. This endpoint stays because
    already-shipped desktop bundles still read it.
    """
    from core.evolution.user_settings import resolve_for_user

    return success_response(data=resolve_for_user(user.user_id))


@router.patch("/settings", summary="更新本账号的进化参与设置")
async def update_evolution_settings(
    body: ContributionSettingRequest,
    user: UserContext = Depends(get_current_user),
):
    """Legacy alias: writes the same switch as PATCH /evolution/prefs."""
    from core.evolution.user_settings import update_for_user

    try:
        return success_response(data=update_for_user(user.user_id, body.enabled))
    except ValueError as exc:
        raise BadRequestError(str(exc))


@router.get("/contributions", summary="查看本账号促成了哪些能力进化")
async def get_contributions(user: UserContext = Depends(get_current_user)):
    """The chain from this user's episodes through to candidates.

    Answers the question the settings panel raises — "I left it on, so what did
    it actually do?" — and reports each candidate's true governance state rather
    than an optimistic one.
    """
    from core.evolution.user_settings import summarise_contributions

    return success_response(data=summarise_contributions(user.user_id))


@router.get("/my-candidates", summary="待我审批的能力候选")
async def list_my_candidates(user: UserContext = Depends(get_current_user)):
    """Candidates this user may accept for themselves.

    Only those whose evidence is mostly their own. A capability learned mainly
    from other people's conversations activates for the whole fleet, and that is
    not one person's call to make.
    """
    from core.evolution.user_settings import OWN_EVIDENCE_THRESHOLD, pending_for_user

    return success_response(
        data={
            "candidates": pending_for_user(user.user_id),
            "own_evidence_threshold": OWN_EVIDENCE_THRESHOLD,
        }
    )


@router.post("/my-candidates/{candidate_id}/approve", summary="批准并为自己启用")
async def approve_my_candidate(
    candidate_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Accept a candidate as a private skill for this user."""
    from core.evolution.activation import ActivationError
    from core.evolution.user_settings import approve_for_user

    try:
        return success_response(data=approve_for_user(candidate_id, user.user_id))
    except PermissionError as exc:
        raise BadRequestError(str(exc))
    except ActivationError as exc:
        raise BadRequestError(f"{exc.code}: {exc.message}")


class PrefsPatch(BaseModel):
    enabled: Optional[bool] = None
    ontology_mode: Optional[str] = Field(None, description="none | any | specific")
    ontology_pack_id: Optional[str] = None
    mechanisms: Optional[list] = None
    min_support: Optional[int] = Field(None, ge=2, le=20)
    auto_approve_low_risk: Optional[bool] = None


@router.get("/prefs", summary="获取我的进化配置")
async def get_prefs(user: UserContext = Depends(get_current_user)):
    """Preferences plus the domain packs available to bind to.

    The pack list ships with the prefs so the UI can render "no packs
    configured" as a visible fact rather than hiding the setting — an absent
    control looks like a missing feature, which is a different message.
    """
    from core.evolution.prefs import (
        ONTOLOGY_MODES,
        available_ontology_packs,
        resolve_prefs,
    )

    return success_response(
        data={
            "prefs": resolve_prefs(user.user_id),
            "ontology_packs": available_ontology_packs(),
            "ontology_modes": list(ONTOLOGY_MODES),
        }
    )


@router.patch("/prefs", summary="更新我的进化配置")
async def update_prefs_api(
    body: PrefsPatch,
    user: UserContext = Depends(get_current_user),
):
    import asyncio

    from core.evolution.prefs import update_prefs

    patch = {k: v for k, v in body.model_dump().items() if v is not None}

    # Turning evolution *on* requires the dependencies it needs to work.
    # Allowing it without them produces a switch that reads "on" while the
    # thing it enables silently does almost nothing — and the user has no way
    # to tell that apart from "my conversations just aren't producing patterns".
    # Turning it off is never blocked.
    if patch.get("enabled") is True:
        from core.evolution.readiness import check_readiness

        readiness = await asyncio.to_thread(check_readiness)
        if not readiness.ready:
            missing = "、".join(
                r.label for r in readiness.requirements if not r.ok
            )
            raise BadRequestError(
                f"当前部署缺少能力进化所需的依赖（{missing}），开启后不会产生任何进化结果。"
                "请先完成配置。"
            )

    try:
        return success_response(data=update_prefs(user.user_id, patch))
    except ValueError as exc:
        raise BadRequestError(str(exc))


@router.post("/my-cycle", summary="立即为我运行一次进化")
async def run_my_cycle(user: UserContext = Depends(get_current_user)):
    """Run this user's own evolution pass now.

    Personal scope, so it needs no administrator: it can only ever mine this
    user's evidence and produce candidates only they can accept.
    """
    from core.evolution.personal import run_personal_cycle

    return success_response(data=run_personal_cycle(user.user_id))
