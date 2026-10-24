"""Shared publication and waiter lifetime."""

from __future__ import annotations
import asyncio
import logging
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from core.llm.runtime import interaction_store
from . import state

from .shared import _load_session_grants, _save_session_grant
from core.llm.tool_permissions.pending import approve_current_policy

logger = logging.getLogger(__name__)


async def _register_pending(
    cid_key: str, key: Tuple[str, ...], *, make_pending, timeout, precheck=None
) -> Tuple[Optional[tuple], Optional[str], Optional[state._Pending]]:
    """Deduplicate within one authority and publish only unanswered confirmations."""
    now = time.monotonic()
    ui_q: Optional[asyncio.Queue] = None
    ui_payload: Optional[Dict[str, Any]] = None
    with state.process_state().LOCK:
        state._gc(now)
        st = state._get_chat(cid_key)
        st.last_ts = now
        if precheck is not None:
            short = precheck(st)
            if short is not None:
                return (short, None, None)
        existing_cid = st.key_index.get(key)
        p: Optional[state._Pending] = st.pending.get(existing_cid) if existing_cid else None
        is_new = p is None or p.decision is not None
        if is_new:
            cid = uuid.uuid4().hex[:12]
            p = make_pending()
            st.pending[cid] = p
            st.key_index[key] = cid
        else:
            cid = existing_cid
        p.waiters += 1
        if is_new:
            if st.ui_signals is None:
                st.ui_signals = asyncio.Queue()
            ui_q = st.ui_signals
            ui_payload = state._pending_to_info(cid, p, include_policy=True)
    try:
        if is_new:
            try:
                await interaction_store.register("confirm", cid_key, cid, ui_payload, timeout)
            except BaseException as exc:
                p.registration_error = exc
                raise
            finally:
                p.registered = True
            approved = await approve_current_policy(cid_key, cid, ui_payload)
            if ui_q is not None and (not approved):
                ui_q.put_nowait({k: v for k, v in ui_payload.items() if not k.startswith("_")})
        else:
            while not p.registered:
                await asyncio.sleep(0.01)
            if p.registration_error is not None:
                raise p.registration_error
    except BaseException:
        with state.process_state().LOCK:
            p.waiters -= 1
            last = p.waiters <= 0
            if last:
                st.pending.pop(cid, None)
                if st.key_index.get(key) == cid:
                    st.key_index.pop(key, None)
        if last:
            await interaction_store.remove("confirm", cid_key, cid)
        raise
    return (None, cid, p)


async def _wait_pending(
    cid_key: str,
    key: Tuple[str, ...],
    cid: str,
    p: state._Pending,
    *,
    timeout: float,
    expire_message: str,
    cancel_message: str,
    in_lock_extract=None,
) -> Tuple[str, Any]:
    """Resolve, claim execution and release the last waiter under one state lock.

    Cancelled waiters release resources without claiming execution."""
    cancelled = False
    wait_error = None
    try:
        if cid_key != "_nochat_" and p.kind != state.KIND_DESIGN_PICK:
            for grant_kind, scope, authority in await _load_session_grants(cid_key):
                if (
                    authority == (p.approval_user_id or "")
                    and grant_kind == p.kind
                    and state._scope_covers(p.kind, scope, p.logical_path)
                ):
                    await interaction_store.decide(
                        "confirm", cid_key, cid, {"decision": state.DECISION_ALLOW_SESSION}
                    )
        remote = await interaction_store.wait(
            "confirm", cid_key, cid, p.event, timeout, policy=approve_current_policy
        )
        if remote is not None and remote.get("decision"):
            state.set_decision(
                cid_key, cid, remote["decision"], remote.get("option_id"), cascade=False
            )
            if remote["decision"] == state.DECISION_ALLOW_SESSION:
                await _save_session_grant(
                    cid_key,
                    p.kind,
                    state._session_scope(p.kind, p.logical_path),
                    p.approval_user_id,
                )
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        cancelled = True
    except Exception as exc:
        wait_error = exc
        cancelled = True
    expire_q: Optional[asyncio.Queue] = None
    expire_payload: Optional[Dict[str, Any]] = None
    with state.process_state().LOCK:
        decision = p.decision or state._DECISION_TIMEOUT
        extracted = in_lock_extract(p, decision) if in_lock_extract and not cancelled else None
        p.waiters -= 1
        if p.waiters <= 0:
            st2 = state.process_state().CHATS.get(cid_key)
            if st2 is not None:
                st2.pending.pop(cid, None)
                if st2.key_index.get(key) == cid:
                    st2.key_index.pop(key, None)
                if st2.ui_signals is not None and (p.kind != state.KIND_DESIGN_PICK or cancelled):
                    expire_q = st2.ui_signals
                    expire_payload = {
                        "confirm_id": cid,
                        "op": p.op,
                        "logical_path": p.logical_path,
                        "kind": p.kind,
                        "expired": True,
                        "resolved": not cancelled and decision != state._DECISION_TIMEOUT,
                        "message": cancel_message if cancelled else expire_message,
                    }
    if expire_q is not None and expire_payload is not None:
        try:
            expire_q.put_nowait(expire_payload)
        except Exception:
            state.logger.warning("[confirm] expire signal put failed", exc_info=True)
    if p.waiters <= 0:
        await interaction_store.remove("confirm", cid_key, cid)
    if wait_error is not None:
        raise wait_error
    if cancelled:
        raise asyncio.CancelledError()
    return (decision, extracted)
