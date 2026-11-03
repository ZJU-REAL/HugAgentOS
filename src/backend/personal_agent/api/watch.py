"""Read-only runtime view for operators and, later, the Watchdog service account."""

from fastapi import APIRouter, Depends

from api.deps import require_admin
from core.db.threads import run_in_session
from core.infra.responses import success_response

router = APIRouter(dependencies=[Depends(require_admin)])


def _snapshot(db):
    from personal_agent.health import board
    from personal_agent.telemetry import summaries

    entries = board(db)
    failing = sorted((e for e in entries if e["failing_since"]), key=lambda e: e["failing_since"])
    return {
        "health": entries,
        "failures": [{"name": e["name"], "owner": e["owner"], "since": e["failing_since"],
                      "count": e["failures"], "detail": e["detail"]} for e in failing],
        "telemetry": summaries(db),
    }


@router.get("/health")
async def health():
    return success_response(data=await run_in_session(_snapshot))
