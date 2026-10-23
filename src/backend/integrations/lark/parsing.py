"""Lark parsing responsibilities."""
from __future__ import annotations
import json
from typing import Any, Dict, Optional
from integrations.lark import state as lark_state

def loads(s: str) -> Any:
    try:
        return json.loads((s or "").strip() or "null")
    except (ValueError, TypeError):
        return None


def parse_device_login(stdout: str) -> Dict[str, Optional[str]]:
    """``auth login --no-wait --json`` → {device_code, user_code, verification_url,
    verification_url_complete}. Field names matched leniently (Lark/OAuth device-flow conventional naming)."""
    out: Dict[str, Optional[str]] = {
        "device_code": None, "user_code": None,
        "verification_url": None, "verification_url_complete": None,
    }
    data = loads(stdout)
    if not isinstance(data, dict):
        return out
    # Some CLIs wrap the content inside data/result
    for key in ("data", "result", "device", "deviceAuth"):
        inner = data.get(key)
        if isinstance(inner, dict):
            data = {**data, **inner}

    def _g(*names: str) -> Optional[str]:
        for n in names:
            for k, v in data.items():
                if k.lower().replace("_", "") == n and isinstance(v, (str, int)):
                    return str(v)
        return None

    out["device_code"] = _g("devicecode")
    out["user_code"] = _g("usercode")
    out["verification_url"] = _g("verificationurl", "verificationuri")
    out["verification_url_complete"] = _g("verificationurlcomplete", "verificationuricomplete")
    return out


def parse_lark_identity(stdout: str) -> Dict[str, Optional[str]]:
    """``auth status``/``auth list`` output → {lark_open_id, lark_name, tenant_key} (searched recursively)."""
    out: Dict[str, Optional[str]] = {"lark_open_id": None, "lark_name": None, "tenant_key": None}
    data = loads(stdout)
    if data is None:
        return out

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = k.lower().replace("_", "")
                if out["lark_open_id"] is None and lk in ("openid", "unionid", "userid"):
                    if isinstance(v, (str, int)):
                        out["lark_open_id"] = str(v)
                if out["lark_name"] is None and lk in ("name", "username", "nick", "ennname", "displayname"):
                    if isinstance(v, str):
                        out["lark_name"] = v
                if out["tenant_key"] is None and lk in ("tenantkey", "tenant"):
                    if isinstance(v, (str, int)):
                        out["tenant_key"] = str(v)
                _walk(v)
        elif isinstance(obj, list):
            for it in obj:
                _walk(it)

    _walk(data)
    return out


def parse_user_identity_state(stdout: str) -> Optional[bool]:
    """``auth status`` → whether the **user** identity is usable right now;
    ``None`` when the output shape is unrecognized (caller falls back to heuristics).

    lark-cli reports the bot and the user identity separately::

        {"identities": {"bot":  {"status": "ready",   "available": true},
                        "user": {"status": "missing", "available": false,
                                 "openId": "ou_…", "userName": "…",
                                 "tokenStatus": "expired"}}}

    Note that ``openId``/``userName`` **stay in the payload after the token
    expires** — they describe who last authorized, not whether the
    authorization still works. So "an identity field is present" must never be
    read as "logged in"; doing so is what let a login that expired months ago
    keep reporting 已登录 on the connection panel while the sandbox correctly
    answered "User identity: missing".
    """
    data = loads(stdout)
    if not isinstance(data, dict):
        return None
    identities = data.get("identities")
    if not isinstance(identities, dict):
        return None
    user = identities.get("user")
    if not isinstance(user, dict):
        return None
    available = user.get("available")
    if isinstance(available, bool):
        return available
    status = str(user.get("status") or "").strip().lower()
    if not status:
        return None
    return status in ("ready", "ok", "active", "valid")


def is_authenticated(stdout: str) -> bool:
    """``auth status`` → whether the user is logged in.

    Prefers the structured ``identities.user`` verdict (see
    [[parse_user_identity_state]]); only when the CLI emits a shape we do not
    recognize does it fall back to the old lenient heuristics — those also have
    to serve ``auth login --device-code`` output, which carries no
    ``identities`` block.
    """
    state = parse_user_identity_state(stdout)
    if state is not None:
        return state
    data = loads(stdout)
    if isinstance(data, dict):
        for k, v in data.items():
            lk = k.lower().replace("_", "")
            if lk in ("authenticated", "loggedin", "isloggedin", "valid") and v is True:
                return True
    ident = parse_lark_identity(stdout)
    return bool(ident.get("lark_open_id") or ident.get("lark_name"))


def classify_login_result(stdout: str, stderr: str, rc: int) -> str:
    """Classify the result of ``auth login --device-code`` → ``ok`` / ``pending`` / ``error``."""
    blob = ((stdout or "") + "\n" + (stderr or "")).lower()
    if rc == 0 and is_authenticated(stdout):
        return "ok"
    if any(m in blob for m in lark_state.PENDING_MARKERS):
        return "pending"
    if any(m in blob for m in lark_state.AUTH_FAIL_MARKERS):
        return "error"
    # rc==0 but no definite identity: the caller decides whether to probe status again; here conservatively treat as pending (not yet terminal)
    return "ok" if rc == 0 else "pending"
