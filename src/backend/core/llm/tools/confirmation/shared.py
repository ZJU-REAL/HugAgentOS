"""Shared confirmation queries and explicit user decisions."""

from __future__ import annotations
import logging
from core.llm.runtime import interaction_store
from . import state

logger = logging.getLogger(__name__)


async def get_all_pending_shared(chat_id):
    return (
        [
            {k: v for k, v in info.items() if not k.startswith("_")}
            for info in await interaction_store.pending("confirm", chat_id)
        ]
        if chat_id
        else []
    )


async def list_pending_chat_ids_shared():
    return await interaction_store.chats("confirm")


async def _save_session_grant(chat_id, kind, scope, authority):
    import hashlib
    import json
    from core.infra.ephemeral import get_ephemeral_state
    from urllib.parse import quote

    value = json.dumps([kind, scope, authority or ""])
    key = (
        "human-grant:v2:"
        + quote(chat_id, safe="")
        + ":"
        + hashlib.sha256(value.encode()).hexdigest()
    )
    await get_ephemeral_state().put(key, value, ttl=state._TTL_S)


async def _load_session_grants(chat_id):
    import json
    from core.infra.ephemeral import get_ephemeral_state
    from urllib.parse import quote

    store = get_ephemeral_state()
    grants = []
    for key in await store.keys("human-grant:v2:" + quote(chat_id, safe="") + ":"):
        raw = await store.get(key)
        if raw:
            grants.append(json.loads(raw))
    return grants


async def set_decision_shared(chat_id, confirm_id, decision, option_id=None):
    record = await interaction_store.read("confirm", chat_id, confirm_id)
    if record is None:
        return {"ok": False, "reason": "stale", "error": "该确认已失效"}
    info = record["info"]
    kind = info["kind"]
    if kind == state.KIND_DESIGN_PICK:
        if decision not in state._DESIGN_DECISIONS:
            return {"ok": False, "reason": "bad_decision", "error": "非法设计选择"}
        if decision == state.DECISION_CHOICE and str(option_id) not in {
            str(option.get("id")) for option in info.get("options", [])
        }:
            return {"ok": False, "reason": "bad_option", "error": "非法设计选项"}
    elif decision not in state._DECISIONS:
        return {"ok": False, "reason": "bad_decision", "error": "非法确认决定"}
    payload = {"decision": decision, "option_id": option_id}
    if not await interaction_store.decide("confirm", chat_id, confirm_id, payload):
        return {"ok": False, "reason": "stale", "error": "该确认已失效"}
    cascaded = []
    if decision == state.DECISION_ALLOW_SESSION:
        scope = state._session_scope(kind, info["logical_path"])
        await _save_session_grant(chat_id, kind, scope, info.get("_approval_user_id"))
        for other in await interaction_store.pending("confirm", chat_id):
            if (
                other["kind"] == kind
                and other.get("_approval_user_id") == info.get("_approval_user_id")
                and state._scope_covers(kind, scope, other["logical_path"])
            ):
                if await interaction_store.decide("confirm", chat_id, other["confirm_id"], payload):
                    cascaded.append(other["confirm_id"])
    return {
        "ok": True,
        "decision": decision,
        "op": info["op"],
        "logical_path": info["logical_path"],
        "cascaded": cascaded,
    }
