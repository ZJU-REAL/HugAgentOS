"""DingTalk account connection service (dingtalk skill / dws CLI).

Drives ``dws auth`` **as the current user** in their dedicated persistent
sandbox to complete the device-flow OAuth login, and persists the connection
status / DingTalk identity summary into the ``dingtalk_connections`` table.
The credentials themselves (token + encrypted keychain) are persisted at
``$STORAGE/dws_cache/{uid}/`` via ``_make_dws_creds_volumes``'s per-user
bind-mount, never stored in the DB. The Custom App's client-id/secret are
injected into the sandbox environment by ``dws_extra_envs``, bypassing the
DingTalk co-creation-period allowlist. Design in
internal design docs.

Note: the device flow's intermediate output format (user_code + short URL)
is authoritative only per real-device testing (P0). The parser
``parse_device_login_output`` in this module is deliberately lenient (it
also returns the raw output), and all sandbox interactions degrade
harmlessly — any step failure sets the connection to error and reports back,
never raising a bare exception that breaks the front end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.db.repository import DingTalkConnectionRepository

logger = logging.getLogger(__name__)

# In-progress device-flow login tasks (user_id → asyncio.Task). Login now runs on the
# **backend** as a subprocess: the process is stable, unaffected by cube's
# "commands.run kills background processes on return", and each user has an isolated HOME.
# The task survives across HTTP requests (same event loop + this module-level reference),
# updates the DB itself on completion, and removes itself from the map.
_LOGIN_TASKS: "dict[str, asyncio.Task]" = {}
_DWS_BIN = "dws"

# Regexes for parsing device-flow output (lenient; tolerates json / plain text / Chinese and English copy).
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
_CODE_RES = (
    re.compile(r'user[_]?code["\s:=]+([A-Za-z0-9][A-Za-z0-9-]{3,15})', re.I),
    re.compile(r'(?:验证码|授权码|码)[：:\s]+([A-Za-z0-9][A-Za-z0-9-]{3,15})'),
)


def dingtalk_login_session_id(user_id: str) -> str:
    return f"dingtalk-{user_id}"


def make_qr_data_uri(url: Optional[str]) -> Optional[str]:
    """Render the verification URL as an SVG QR-code data-URI (for scan-to-bind).
    segno is pure Python with zero extra dependencies; if the library is
    missing or url is empty, return None and the front end falls back to
    "click the link + copy the user code"."""
    if not url:
        return None
    try:
        import segno

        return segno.make(url, error="m").svg_data_uri(scale=5, border=2)
    except Exception as exc:  # noqa: BLE001
        logger.info("[dingtalk] qr render failed: %s", exc)
        return None


def parse_device_login_output(
    text: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (verification URL, user_code, complete URL) from the output of ``dws auth login --device``.

    dws device-code box format (``dfPrintDeviceCodeBox``), with Chinese labels verbatim:
        链接: <verificationUri>                    (link)
        授权码: <userCode>                          (user code)
        或者直接打开以下链接：                       (or open this link directly:)
          <verificationUriComplete>     ← code already embedded, one scan goes straight through
    The third item, ``verification_uri_complete``, is the QR-code target for
    **scan-to-bind** (preferred over the plain URL). Returns None for any
    item that cannot be extracted; the caller still returns the raw output
    to the front end as a fallback.
    """
    if not text:
        return None, None, None
    urls = [u.rstrip(".,;") for u in _URL_RE.findall(text)]
    url = urls[0] if urls else None
    code: Optional[str] = None
    for rx in _CODE_RES:
        m = rx.search(text)
        if m:
            code = m.group(1)
            break
    # Complete URL: prefer the one with the user_code embedded; otherwise take the second one (in the box, complete comes after plain)
    complete: Optional[str] = None
    if code:
        for u in urls:
            if code in u or "user_code=" in u or "userCode=" in u:
                complete = u
                break
    if complete is None and len(urls) >= 2:
        complete = urls[1]
    return url, code, complete


def parse_auth_status(stdout: str) -> bool:
    """``dws auth status -f json`` → whether authenticated. Any parse failure counts as not authenticated."""
    try:
        data = json.loads((stdout or "").strip() or "{}")
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and bool(data.get("authenticated"))


def parse_get_self(stdout: str) -> Dict[str, Optional[str]]:
    """``dws contact user get-self -f json`` → {dingtalk_user_id, dingtalk_name, corp_id}。

    Best effort: extract what it can when the structure changes, leave None
    when unavailable, never raise.
    """
    out: Dict[str, Optional[str]] = {"dingtalk_user_id": None, "dingtalk_name": None, "corp_id": None}
    try:
        data = json.loads((stdout or "").strip() or "{}")
    except (ValueError, TypeError):
        return out

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = k.lower()
                if out["dingtalk_user_id"] is None and lk in ("userid", "user_id", "unionid"):
                    if isinstance(v, (str, int)):
                        out["dingtalk_user_id"] = str(v)
                if out["dingtalk_name"] is None and lk in ("orgusername", "name", "nick", "username"):
                    if isinstance(v, str):
                        out["dingtalk_name"] = v
                if out["corp_id"] is None and lk in ("corpid", "corp_id"):
                    if isinstance(v, (str, int)):
                        out["corp_id"] = str(v)
                _walk(v)
        elif isinstance(obj, list):
            for it in obj:
                _walk(it)

    _walk(data)
    return out


def _dws_env(user_id: str) -> Dict[str, str]:
    """Environment for running dws subprocesses on the backend: per-user
    isolated ``HOME`` (the root of credential isolation) + deployment-level
    Custom App client credentials + domain whitelist. Multi-tenant isolation
    is guaranteed by the per-user HOME."""
    from core.sandbox._common import dws_extra_envs, dws_home_dir

    home = dws_home_dir(user_id)
    try:
        home.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dingtalk] mkdir home %s failed: %s", home, exc)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.update(dws_extra_envs())
    return env


async def _run_dws(user_id: str, args: List[str], timeout: int = 40) -> Tuple[str, str, int]:
    """Run one non-blocking dws subcommand on the backend as this user; returns (stdout, stderr, exit_code)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _DWS_BIN, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_dws_env(user_id),
        )
    except FileNotFoundError:
        return "", "dws binary not found on backend", 127
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return "", f"dws {' '.join(args[:2])} timeout", 124


# The three login-state fields: filled while pending, cleared on connect success/failure/disconnect.
# Centralized as a constant to avoid missing one of the 5 hand-copied sites.
_CLEAR_LOGIN: Dict[str, Any] = {
    "login_verification_url": None,
    "login_verification_url_complete": None,
    "login_user_code": None,
}


def _update_connection(user_id: str, data: Dict[str, Any]) -> None:
    """Update the connection record with an independent DB session (for the background login task — must not borrow the request-scoped session)."""
    from core.db.engine import SessionLocal

    try:
        with SessionLocal() as db:
            DingTalkConnectionRepository(db).update(user_id, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dingtalk] _update_connection user=%s failed: %s", user_id, exc)


async def _build_connected_update(user_id: str) -> Dict[str, Any]:
    """After successful login, backfill the DingTalk identity + export a portable
    credential bundle, returning the update dict for the ``connected`` state.
    Shared by _device_login_flow (background session) and the poll_login
    fallback (request session), avoiding two copies of the same
    "get-self → export → 11-key connected dict" block."""
    who, _, _ = await _run_dws(user_id, ["contact", "user", "get-self", "-f", "json"])
    ident = parse_get_self(who)
    bout, _, brc = await _run_dws(user_id, ["auth", "export", "--base64"], timeout=30)
    return {
        "status": "connected",
        "dingtalk_user_id": ident.get("dingtalk_user_id"),
        "dingtalk_name": ident.get("dingtalk_name"),
        "corp_id": ident.get("corp_id"),
        "auth_bundle": bout.strip() if (brc == 0 and bout.strip()) else None,
        **_CLEAR_LOGIN,
        "last_verified_at": datetime.utcnow(),
        "last_error": None,
    }


# Unified copy reported to the front end after the DingTalk login becomes invalid (refresh token rotated by the server / expired).
_EXPIRED_ERROR = "钉钉登录已失效（令牌被刷新或过期），请到设置页重新连接钉钉"

# Markers in get-self errors that positively confirm "auth invalid". All other errors (rate limiting / network / server 5xx) draw no conclusion.
_AUTH_FAIL_REASONS = {
    "not_authenticated",
    "token_expired",
    "invalid_grant",
    "refresh_failed",
    "unauthorized",
    "login_required",
}


async def verify_and_refresh(user_id: str) -> str:
    """Verify with a **real API call** whether this user's backend-HOME credentials are still valid; returns ``valid``/``invalid``/``unknown``.

    Why not ``dws auth status``: it only makes a **local** judgment (reports
    ``authenticated: true`` as long as the refresh token's 30-day window has
    not elapsed), and cannot detect a refresh token that the server has
    **rotated and invalidated** — in real testing it falsely reports dead
    credentials as valid. So a real request (``contact user get-self``) is
    mandatory:
      - Success → ``valid``. And if the access token has expired, this call
        makes the **backend HOME exchange it in place with the refresh
        token and rotate** — which is exactly "single-holder refresh": the
        rotation happens on the backend's own copy and affects nothing else.
      - Explicit auth error (category=auth / reason∈_AUTH_FAIL_REASONS) →
        ``invalid``; the caller persists disconnected accordingly and
        prompts the user to reconnect.
      - Everything else (rate limiting / network / timeout / dws missing /
        unparseable) → ``unknown``, **no conclusion drawn**, so a single
        network blip cannot condemn the user's connection.
    """
    out, err, _ = await _run_dws(
        user_id, ["contact", "user", "get-self", "-f", "json"], timeout=25
    )
    blob = (out or "").strip() or (err or "").strip()
    if not blob:
        return "unknown"
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return "unknown"
    if isinstance(data, dict):
        errobj = data.get("error")
        if isinstance(errobj, dict):
            category = str(errobj.get("category", "")).lower()
            reason = str(errobj.get("reason", "")).lower()
            if category == "auth" or reason in _AUTH_FAIL_REASONS:
                return "invalid"
            return "unknown"  # non-auth error, no conclusion drawn
    ident = parse_get_self(blob)
    if ident.get("dingtalk_user_id") or ident.get("dingtalk_name"):
        return "valid"
    return "unknown"


def mark_login_expired(user_id: str) -> None:
    """Persist the connection as disconnected + the expiry copy (background /
    out-of-request paths use an independent session). Identity fields are
    kept so the front end can show "<name> · expired, please reconnect"."""
    _update_connection(
        user_id, {"status": "disconnected", **_CLEAR_LOGIN, "last_error": _EXPIRED_ERROR}
    )


async def _device_login_flow(user_id: str) -> None:
    """Backend long-running task: runs the full ``dws auth login --device``
    flow (stream-read the URL → wait for user approval → save the token to
    disk → backfill identity + export the credential bundle). The process is
    a backend subprocess, stably alive, unaffected by sandbox process
    killing. Per-user isolated HOME → credential isolation. This function
    manages its own DB sessions and exceptions and never raises into the
    event loop."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _DWS_BIN, "auth", "login", "--device", "--force",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_dws_env(user_id),
        )
    except FileNotFoundError:
        _update_connection(user_id, {"status": "error", "last_error": "后端未安装 dws"})
        return

    buf = ""
    pushed = False
    try:
        # Stream-read the output. dws prints the entire device-code box in one go
        # (link / user code / the "or open directly" complete URL / the "expires in
        # N seconds" line) before blocking on polling. We must wait until the
        # **whole box is read** before parsing + updating the DB, otherwise — as in
        # the early version — we would only grab the plain URL on the first line
        # and miss the user_code and the code-embedded complete URL (the QR target).
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=600)
            except asyncio.TimeoutError:
                break
            if not line:
                break  # EOF → process exited (login completed or failed)
            buf += line.decode("utf-8", "replace")
            if not pushed:
                u, c, comp = parse_device_login_output(buf)
                # Box-complete markers: the trailing "过期/expire" line appears, or both URL + code are in place
                box_done = ("过期" in buf) or ("expire" in buf.lower()) or (u and c)
                if box_done and (u or c):
                    _update_connection(user_id, {
                        "status": "pending",
                        "login_verification_url": u,
                        "login_verification_url_complete": comp,
                        "login_user_code": c,
                        "login_started_at": datetime.utcnow(),
                        "last_error": None,
                    })
                    pushed = True
        await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dingtalk] device login stream failed user=%s: %s", user_id, exc)
        _update_connection(user_id, {"status": "error", "last_error": f"登录流异常: {exc}"})
        return
    finally:
        _LOGIN_TASKS.pop(user_id, None)

    # Process exited → verify whether login actually succeeded
    out, _, _ = await _run_dws(user_id, ["auth", "status", "-f", "json"])
    if not parse_auth_status(out):
        tail = buf.strip()[-400:]
        _update_connection(user_id, {
            "status": "error",
            **_CLEAR_LOGIN,
            "last_error": f"授权未完成或已过期，请重试。{('详情: ' + tail) if tail else ''}",
        })
        return
    # Authenticated → backfill the DingTalk identity + export the portable credential bundle (for per-user injection into cube session sandboxes)
    _update_connection(user_id, await _build_connected_update(user_id))


class DingTalkService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = DingTalkConnectionRepository(db)

    def _fresh(self, user_id: str):
        """Discard the session cache and re-read — to see the latest state committed by the background login task's independent session."""
        self.db.expire_all()
        return self.repo.ensure(user_id)

    # ── Status ───────────────────────────────────────────────────────────
    def status_dict(self, record) -> Dict[str, Any]:
        # The QR code prefers the code-embedded complete URL (one scan goes straight to the authorization page), otherwise falls back to the plain URL.
        qr_target = record.login_verification_url_complete or record.login_verification_url
        return {
            "status": record.status,
            "dingtalk_user_id": record.dingtalk_user_id,
            "dingtalk_name": record.dingtalk_name,
            "corp_id": record.corp_id,
            "granted_scopes": list(record.granted_scopes or []),
            "verification_url": record.login_verification_url,
            "verification_url_complete": record.login_verification_url_complete,
            "qr_data_uri": make_qr_data_uri(qr_target) if record.status == "pending" else None,
            "user_code": record.login_user_code,
            "last_verified_at": record.last_verified_at.isoformat() if record.last_verified_at else None,
            "last_error": record.last_error,
        }

    def get_status(self, user_id: str) -> Dict[str, Any]:
        record = self.repo.ensure(user_id)
        return self.status_dict(record)

    async def probe_status(self, user_id: str) -> Dict[str, Any]:
        """Backend **real-API liveness probe** reconciliation (corrects mismatches between the DB and actual credentials).

        Uses :func:`verify_and_refresh` (a real request) instead of the local
        ``dws auth status`` — the latter falsely reports refresh tokens that
        the server has rotated and invalidated as valid, so the front end
        keeps showing "connected" while every actual call fails. On
        ``unknown`` (network / rate limiting etc., no conclusion) the DB is
        left untouched, avoiding misjudgment from a blip.
        """
        record = self._fresh(user_id)
        # Do not interrupt an in-progress login task; just echo the current (pending) state
        if record.status == "pending" and user_id in _LOGIN_TASKS:
            return self.status_dict(record)
        verdict = await verify_and_refresh(user_id)
        if verdict == "valid" and record.status != "connected":
            self.repo.update(user_id, {"status": "connected", "last_error": None})
        elif verdict == "invalid" and record.status == "connected":
            self.repo.update(user_id, {"status": "disconnected", "last_error": _EXPIRED_ERROR})
        return self.status_dict(self.repo.get(user_id))

    # ── Device-flow login (backend subprocess, process stably alive) ─────
    async def start_device_login(self, user_id: str) -> Dict[str, Any]:
        """Start the ``dws auth login --device`` long-running task on the **backend**, and return once the URL/QR code appears.

        The process is a backend subprocess (not sandboxed), stably alive
        until the user approves; the front end presents the URL/QR code to
        the user and then polls :meth:`poll_login` until connected. Per-user
        isolated HOME → isolation.
        """
        self.repo.ensure(user_id)
        # Cancel the same user's old task, reset to pending
        old = _LOGIN_TASKS.pop(user_id, None)
        if old is not None and not old.done():
            old.cancel()
        self.repo.update(user_id, {
            "status": "pending",
            **_CLEAR_LOGIN,
            "login_started_at": datetime.utcnow(),
            "last_error": None,
        })
        # Start the background login task (manages its own independent DB session)
        _LOGIN_TASKS[user_id] = asyncio.create_task(_device_login_flow(user_id))
        # Wait up to ~15s for the verification URL to appear (the task writes with an independent session; here we expire then re-read)
        for _ in range(30):
            await asyncio.sleep(0.5)
            rec = self._fresh(user_id)
            if rec.login_verification_url or rec.status in ("connected", "error"):
                break
        return self.status_dict(self._fresh(user_id))

    async def poll_login(self, user_id: str) -> Dict[str, Any]:
        """Poll login progress. The background task drives the DB to
        connected/error; this only reads the latest state. If the task is
        gone but still pending (e.g. a backend restart lost the task), fall
        back to a single status probe."""
        record = self._fresh(user_id)
        if record.status == "pending" and user_id not in _LOGIN_TASKS:
            out, _, _ = await _run_dws(user_id, ["auth", "status", "-f", "json"])
            if parse_auth_status(out):
                self.repo.update(user_id, await _build_connected_update(user_id))
                record = self.repo.get(user_id)
        return self.status_dict(record)

    # ── Disconnect ───────────────────────────────────────────────────────
    async def disconnect(self, user_id: str) -> Dict[str, Any]:
        """Cancel any in-progress login + backend logout + clear the backend persistent credential directory + set disconnected."""
        old = _LOGIN_TASKS.pop(user_id, None)
        if old is not None and not old.done():
            old.cancel()
        await _run_dws(user_id, ["auth", "logout", "-y"], timeout=30)
        # Clear the backend per-user credential directory (including home/.dws and home/.local/share/dws-cli)
        try:
            from core.sandbox._common import dws_cache_dir, safe_user_id

            if safe_user_id(user_id):
                shutil.rmtree(dws_cache_dir(user_id), ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[dingtalk] disconnect cache rmtree failed user=%s: %s", user_id, exc)
        self.repo.update(
            user_id,
            {
                "status": "disconnected",
                "dingtalk_user_id": None,
                "dingtalk_name": None,
                "corp_id": None,
                "granted_scopes": [],
                **_CLEAR_LOGIN,
                "auth_bundle": None,
                "last_error": None,
            },
        )
        return self.status_dict(self.repo.get(user_id))
