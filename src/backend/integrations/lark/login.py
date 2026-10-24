"""Lark login responsibilities."""
from __future__ import annotations
from core.infra.time import utc_now
import asyncio
from typing import Any, Dict
from integrations.lark.repository import LarkConnectionRepository
import logging
logger = logging.getLogger(__name__)
from integrations.lark import parsing as lark_parsing
from integrations.lark import process as lark_process
from integrations.lark import state as lark_state

def update_connection(user_id: str, data: Dict[str, Any]) -> None:
    """Update the connection record with an independent DB session (for the background login task — must not borrow the request-scoped session)."""
    from core.db.engine import SessionLocal

    try:
        with SessionLocal() as db:
            LarkConnectionRepository(db).update(user_id, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] _update_connection user=%s failed: %s", user_id, exc)


async def build_connected_update(user_id: str) -> Dict[str, Any]:
    """After successful login, backfill the Lark identity and return the update dict for the ``connected`` state."""
    # No ``--format json``: no shipped lark-cli accepts that flag (1.0.34 has only
    # ``--verify``; 1.0.78 spells it ``--json``), and passing it made every call return a
    # "unknown flag" error instead of the status — which is why lark_open_id / lark_name
    # stayed NULL on every connection row. Bare ``auth status`` already prints JSON on both.
    who, _, _ = await lark_process.run_lark(user_id, ["auth", "status"], timeout=20)
    ident = lark_parsing.parse_lark_identity(who)
    return {
        "status": "connected",
        "lark_open_id": ident.get("lark_open_id"),
        "lark_name": ident.get("lark_name"),
        "tenant_key": ident.get("tenant_key"),
        **lark_state.CLEAR_LOGIN,
        "last_verified_at": utc_now(),
        "last_error": None,
    }


async def verify_and_refresh(user_id: str) -> str:
    """Real liveness probe: ``auth status --verify`` (validates against the server,
    renewing with the refresh token along the way).
    Returns ``valid`` / ``invalid`` / ``unknown``. Network/timeout etc. draw no
    conclusion (unknown), avoiding misjudgment from a blip."""
    out, err, rc = await lark_process.run_lark(
        user_id, ["auth", "status", "--verify"], timeout=25
    )
    blob = ((out or "") + "\n" + (err or "")).strip()
    if not blob or rc == 124 or rc == 127:
        return "unknown"
    # The structured verdict is authoritative when the CLI gives us one: substring
    # markers are a poor judge here because a *healthy* status payload also carries
    # words like "expiresAt", and an expired one still carries openId/userName.
    state = lark_parsing.parse_user_identity_state(out)
    if state is not None:
        return "valid" if state else "invalid"
    low = blob.lower()
    if any(m in low for m in lark_state.AUTH_FAIL_MARKERS) or "not authenticated" in low or "not logged in" in low:
        return "invalid"
    if lark_parsing.is_authenticated(out):
        return "valid"
    return "unknown"


async def device_complete_flow(user_id: str, device_code: str) -> None:
    """Backend long-running task: ``auth login --device-code <code>`` polls to
    completion (blocking until authorized/expired), then backfills identity.
    Manages its own DB sessions and exceptions, never raising into the event loop."""
    try:
        out, err, rc = await lark_process.run_lark(
            user_id,
            ["auth", "login", "--device-code", device_code, "--json"],
            timeout=600,
        )
        verdict = lark_parsing.classify_login_result(out, err, rc)
        if verdict == "ok":
            # Confirm login with another status check (fallback when --device-code succeeds but the json has no identity)
            st, _, _ = await lark_process.run_lark(user_id, ["auth", "status"], timeout=20)
            # ``rc == 0`` alone must not win: the CLI can exit 0 without ever storing a
            # user token, and that is how connection rows ended up marked ``connected``
            # with a NULL identity while the sandbox saw no token at all. A *definite*
            # negative from the structured status therefore overrides it; an
            # inconclusive status keeps the old lenient behaviour so unfamiliar CLI
            # output shapes do not start failing valid logins.
            st_state = lark_parsing.parse_user_identity_state(st)
            if st_state is False:
                verdict = "error"
            elif st_state is True or lark_parsing.is_authenticated(st) or rc == 0:
                update_connection(user_id, await build_connected_update(user_id))
                return
            else:
                verdict = "error"
        if verdict == "error":
            tail = ((err or out or "").strip())[-300:]
            update_connection(user_id, {
                "status": "error", **lark_state.CLEAR_LOGIN,
                "last_error": f"扫码授权未完成或已过期，请重试。{('详情: ' + tail) if tail else ''}",
            })
            return
        # pending reaching here (timed out unauthorized) → set error so the user can retry
        update_connection(user_id, {
            "status": "error", **lark_state.CLEAR_LOGIN,
            "last_error": "扫码授权超时，请重新发起连接。",
        })
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] device complete failed user=%s: %s", user_id, exc)
        update_connection(user_id, {"status": "error", "last_error": f"登录异常: {exc}"})
    finally:
        lark_state.LOGIN_TASKS.pop(user_id, None)
