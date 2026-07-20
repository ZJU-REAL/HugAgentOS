"""Lark (Feishu) account connection service (feishu-cli plugin / lark-cli).

Drives ``lark-cli auth`` **as the current user** in their dedicated backend
HOME to complete the device-flow OAuth login (scan-to-bind), and persists
the connection status / Lark identity summary into the ``lark_connections``
table. The credentials themselves (user_access_token + file-based encrypted
store, including master.key) are persisted at ``$STORAGE/lark_cache/{uid}/``
via ``_make_lark_creds_volumes``'s per-user bind-mount, never stored in the
DB.

Structurally identical to dingtalk_service, with two differences (Lark is
easier + Block B decision):
  1. Device flow: ``lark-cli auth login --no-wait --json`` returns a
     structured device_code + verification_url **immediately** (no need to
     stream-parse a text box like dws); the device_code is persisted, then
     ``--device-code`` polls to completion ("poll and complete", a single
     call blocking until authorized/expired).
  2. app_id/app_secret are **not** injected as env (with ``LARK_APP_*`` in
     the environment, lark-cli defaults to the tenant/bot token flow,
     breaking ``--as user``). Instead, before login, ``config init
     --app-secret-stdin`` seeds the app config into the per-user HOME
     (which enters the sandbox together with the credential volume).

Note: the JSON shapes of the device flow / auth status are authoritative
only per real-device testing (P0). Parsing in this module is deliberately
lenient and all subprocess interactions degrade harmlessly — any step
failure sets the connection to error / returns a hint, never raising a bare
exception that breaks the front end.
See internal design docs.
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

from core.db.repository import LarkConnectionRepository
# QR rendering is identical to dingtalk's (segno SVG data-uri); reuse it directly to avoid duplicating the implementation.
from core.services.dingtalk_service import make_qr_data_uri

logger = logging.getLogger(__name__)

# In-progress device-flow completion tasks (user_id → asyncio.Task). ``--device-code``
# blocks polling until the user authorizes / it expires; run as a backend subprocess,
# surviving across HTTP requests, updating the DB itself on completion and removing
# itself from the map.
_LOGIN_TASKS: "dict[str, asyncio.Task]" = {}
_LARK_BIN = "lark-cli"

# Default authorization scopes requested by the device flow: --recommend requests only the
# recommended (auto-approvable) scopes, following least privilege; when a business command
# hits insufficient permissions, the lark-shared skill guides the user to an incremental auth login.
_DEFAULT_LOGIN_ARGS = ["auth", "login", "--recommend", "--no-wait", "--json"]

# The four login-state fields: filled while pending, cleared on connect success/failure/disconnect.
# Centralized as a constant to avoid missing one of the hand-copied sites.
_CLEAR_LOGIN: Dict[str, Any] = {
    "login_verification_url": None,
    "login_verification_url_complete": None,
    "login_user_code": None,
    "login_device_code": None,
}

_EXPIRED_ERROR = "飞书登录已失效（令牌被刷新或过期），请到设置页重新扫码连接飞书"
_NO_APP_ERROR = "飞书应用尚未配置，请联系管理员在「插件库 → 飞书工作台」初始化飞书应用"

# Markers in auth/login results that positively confirm "auth invalid / device code terminated".
_AUTH_FAIL_MARKERS = (
    "expired_token", "access_denied", "invalid_grant", "invalid_device_code",
    "authorization_expired", "expired", "denied",
)
_PENDING_MARKERS = ("authorization_pending", "pending", "slow_down")


# ── JSON parsing (lenient, best effort) ─────────────────────────────────────────

def _loads(s: str) -> Any:
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
    data = _loads(stdout)
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
    data = _loads(stdout)
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


def is_authenticated(stdout: str) -> bool:
    """``auth status`` → whether logged in. Lenient: look for authenticated/loggedIn=true or the presence of identity fields."""
    data = _loads(stdout)
    if isinstance(data, dict):
        for k, v in data.items():
            lk = k.lower().replace("_", "")
            if lk in ("authenticated", "loggedin", "isloggedin", "valid") and v is True:
                return True
    ident = parse_lark_identity(stdout)
    return bool(ident.get("lark_open_id") or ident.get("lark_name"))


def _classify_login_result(stdout: str, stderr: str, rc: int) -> str:
    """Classify the result of ``auth login --device-code`` → ``ok`` / ``pending`` / ``error``."""
    blob = ((stdout or "") + "\n" + (stderr or "")).lower()
    if rc == 0 and is_authenticated(stdout):
        return "ok"
    if any(m in blob for m in _PENDING_MARKERS):
        return "pending"
    if any(m in blob for m in _AUTH_FAIL_MARKERS):
        return "error"
    # rc==0 but no definite identity: the caller decides whether to probe status again; here conservatively treat as pending (not yet terminal)
    return "ok" if rc == 0 else "pending"


# ── Backend subprocess environment / invocation ─────────────────────────────────

def _lark_env(user_id: str) -> Dict[str, str]:
    """Environment for running lark-cli subprocesses on the backend: per-user isolated ``HOME`` (the root of credential isolation).

    LARK_APP_ID/SECRET are **not** injected (with them in the env, lark-cli
    defaults to the tenant/bot token flow, breaking --as user); the app
    config lands in HOME via config init. OPENCLAW_HOME/HERMES_HOME are
    stripped — otherwise ``config init`` decides it is in an Agent context
    and refuses to run."""
    from core.sandbox._common import lark_home_dir

    home = lark_home_dir(user_id)
    try:
        home.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] mkdir home %s failed: %s", home, exc)
    env = dict(os.environ)
    env["HOME"] = str(home)
    for k in ("OPENCLAW_HOME", "HERMES_HOME", "LARK_APP_ID", "LARK_APP_SECRET"):
        env.pop(k, None)
    return env


async def _run_lark(
    user_id: str, args: List[str], timeout: int = 40, stdin_data: Optional[str] = None,
) -> Tuple[str, str, int]:
    """Run one lark-cli subcommand on the backend as this user; returns (stdout, stderr, exit_code)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _LARK_BIN, *args,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_lark_env(user_id),
        )
    except FileNotFoundError:
        return "", "lark-cli binary not found on backend", 127
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=stdin_data.encode("utf-8") if stdin_data is not None else None),
            timeout=timeout,
        )
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return "", f"lark-cli {' '.join(args[:2])} timeout", 124


def _lark_app_config() -> Tuple[Optional[str], Optional[str]]:
    """Read the app's app_id / app_secret from "System Config → Lark Workspace" (deployment-level, one app shared by everyone)."""
    try:
        from core.services.system_config import SystemConfigService

        svc = SystemConfigService.get_instance()
        app_id = (svc.get("lark.app_id") or "").strip()
        app_secret = (svc.get("lark.app_secret") or "").strip()
        return (app_id or None), (app_secret or None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] read lark config failed: %s", exc)
        return None, None


# The app base's two on-disk roots (config init writes both; copying them into the user HOME enables their auth login)
_APP_CFG_SUB = ".lark-cli/config.json"
_APP_STORE_SUB = ".local/share/lark-cli"


def _app_configured() -> Optional[str]:
    """Whether the shared app has been initialized (the admin's one-click config init --new completed). Returns the app_id or None."""
    from core.sandbox._common import lark_app_home_dir

    cfg = lark_app_home_dir() / _APP_CFG_SUB
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        apps = data.get("apps") or []
        if apps and isinstance(apps[0], dict):
            return apps[0].get("appId")
    except Exception:  # noqa: BLE001
        pass
    return None


def _seed_user_from_shared_app(user_id: str) -> bool:
    """Copy the shared app base (config.json + master.key + appsecret_*.enc) into the user HOME.

    Copies only once, when the user HOME has no config.json yet (persisted
    afterwards via the bind-mount). master.key and appsecret are copied
    together so that user tokens written by this user's auth login are
    encrypted with — and decryptable by — the same key.
    User token files are never touched (those belong to each individual)."""
    import shutil

    from core.sandbox._common import lark_app_home_dir, lark_home_dir

    app_home = lark_app_home_dir()
    user_home = lark_home_dir(user_id)
    user_cfg = user_home / _APP_CFG_SUB
    if user_cfg.exists():
        return True  # base already present
    src_cfg = app_home / _APP_CFG_SUB
    if not src_cfg.exists():
        return False
    try:
        user_cfg.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_cfg, user_cfg)
        src_store = app_home / _APP_STORE_SUB
        dst_store = user_home / _APP_STORE_SUB
        dst_store.mkdir(parents=True, exist_ok=True)
        for name in os.listdir(src_store):
            if name == "master.key" or name.startswith("appsecret_"):
                shutil.copy2(src_store / name, dst_store / name)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] seed user app from shared failed user=%s: %s", user_id, exc)
        return False


async def _ensure_app_config(user_id: str) -> Optional[str]:
    """Ensure the user HOME has the app base. Returns an error message; None means ready.

    Prefer the shared app from the **admin's one-click initialization**
    (copying the base into the user HOME; the whole group shares one app);
    without a shared app, fall back to the app_id/secret entered in **System
    Config** (``config init --app-secret-stdin``); with neither → return a
    hint message (the front end guides the admin to initialize).
    """
    # 1) Shared app (admin one-click initialization)
    if _app_configured():
        if _seed_user_from_shared_app(user_id):
            return None
    # 2) app_id/secret hand-entered in System Config (fallback)
    app_id, app_secret = _lark_app_config()
    if app_id and app_secret:
        shown, _, _ = await _run_lark(user_id, ["config", "show"], timeout=15)
        if app_id in (shown or ""):
            return None
        _, err, rc = await _run_lark(
            user_id,
            ["config", "init", "--app-id", app_id, "--app-secret-stdin", "--brand", "feishu"],
            timeout=30,
            stdin_data=app_secret,
        )
        if rc != 0:
            logger.warning("[lark] config init failed user=%s rc=%s err=%s", user_id, rc, (err or "")[:200])
            return f"飞书应用配置初始化失败：{(err or '').strip()[:160] or '未知错误'}"
        return None
    return _NO_APP_ERROR


def _update_connection(user_id: str, data: Dict[str, Any]) -> None:
    """Update the connection record with an independent DB session (for the background login task — must not borrow the request-scoped session)."""
    from core.db.engine import SessionLocal

    try:
        with SessionLocal() as db:
            LarkConnectionRepository(db).update(user_id, data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] _update_connection user=%s failed: %s", user_id, exc)


async def _build_connected_update(user_id: str) -> Dict[str, Any]:
    """After successful login, backfill the Lark identity and return the update dict for the ``connected`` state."""
    who, _, _ = await _run_lark(user_id, ["auth", "status", "--format", "json"], timeout=20)
    ident = parse_lark_identity(who)
    return {
        "status": "connected",
        "lark_open_id": ident.get("lark_open_id"),
        "lark_name": ident.get("lark_name"),
        "tenant_key": ident.get("tenant_key"),
        **_CLEAR_LOGIN,
        "last_verified_at": datetime.utcnow(),
        "last_error": None,
    }


async def verify_and_refresh(user_id: str) -> str:
    """Real liveness probe: ``auth status --verify`` (validates against the server,
    renewing with the refresh token along the way).
    Returns ``valid`` / ``invalid`` / ``unknown``. Network/timeout etc. draw no
    conclusion (unknown), avoiding misjudgment from a blip."""
    out, err, rc = await _run_lark(
        user_id, ["auth", "status", "--verify", "--format", "json"], timeout=25
    )
    blob = ((out or "") + "\n" + (err or "")).strip()
    if not blob or rc == 124 or rc == 127:
        return "unknown"
    low = blob.lower()
    if any(m in low for m in _AUTH_FAIL_MARKERS) or "not authenticated" in low or "not logged in" in low:
        return "invalid"
    if is_authenticated(out):
        return "valid"
    return "unknown"


async def _device_complete_flow(user_id: str, device_code: str) -> None:
    """Backend long-running task: ``auth login --device-code <code>`` polls to
    completion (blocking until authorized/expired), then backfills identity.
    Manages its own DB sessions and exceptions, never raising into the event loop."""
    try:
        out, err, rc = await _run_lark(
            user_id,
            ["auth", "login", "--device-code", device_code, "--json"],
            timeout=600,
        )
        verdict = _classify_login_result(out, err, rc)
        if verdict == "ok":
            # Confirm login with another status check (fallback when --device-code succeeds but the json has no identity)
            st, _, _ = await _run_lark(user_id, ["auth", "status", "--format", "json"], timeout=20)
            if is_authenticated(st) or rc == 0:
                _update_connection(user_id, await _build_connected_update(user_id))
                return
            verdict = "error"
        if verdict == "error":
            tail = ((err or out or "").strip())[-300:]
            _update_connection(user_id, {
                "status": "error", **_CLEAR_LOGIN,
                "last_error": f"扫码授权未完成或已过期，请重试。{('详情: ' + tail) if tail else ''}",
            })
            return
        # pending reaching here (timed out unauthorized) → set error so the user can retry
        _update_connection(user_id, {
            "status": "error", **_CLEAR_LOGIN,
            "last_error": "扫码授权超时，请重新发起连接。",
        })
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] device complete failed user=%s: %s", user_id, exc)
        _update_connection(user_id, {"status": "error", "last_error": f"登录异常: {exc}"})
    finally:
        _LOGIN_TASKS.pop(user_id, None)


# ── Admin one-click initialization of the shared app (config init --new) ────────
# The whole group shares one app, initialized only once, hence a single module-level task + state (not per-user).
_APP_INIT_TASK: "Optional[asyncio.Task]" = None
_app_init_state: Dict[str, Any] = {
    "status": "idle", "verification_url": None, "qr_data_uri": None, "error": None,
}


def _app_env() -> Dict[str, str]:
    """Environment for running the shared app's config init: HOME = the shared app HOME; Agent/LARK_APP_* interference stripped."""
    from core.sandbox._common import lark_app_home_dir

    home = lark_app_home_dir()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] mkdir app home failed: %s", exc)
    env = dict(os.environ)
    env["HOME"] = str(home)
    for k in ("OPENCLAW_HOME", "HERMES_HOME", "LARK_APP_ID", "LARK_APP_SECRET"):
        env.pop(k, None)
    return env


async def _app_init_flow() -> None:
    """Backend long-running task: ``config init --new`` stream-reads the
    ``open.feishu.cn/page/cli`` configuration URL → echoes it back for the
    admin to scan → blocks while the admin completes it in the browser →
    the app base lands in the shared HOME. Manages its own exceptions."""
    global _APP_INIT_TASK
    try:
        proc = await asyncio.create_subprocess_exec(
            _LARK_BIN, "config", "init", "--new", "--lang", "zh",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_app_env(),
        )
    except FileNotFoundError:
        _app_init_state.update({"status": "error", "error": "后端未安装 lark-cli"})
        _APP_INIT_TASK = None
        return
    buf = ""
    try:
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=600)
            except asyncio.TimeoutError:
                break
            if not line:
                break
            buf += line.decode("utf-8", "replace")
            if _app_init_state.get("verification_url") is None:
                m = re.search(r"https?://open\.feishu\.cn/\S+", buf)
                if m:
                    url = m.group(0).rstrip(".,;")
                    _app_init_state.update({
                        "status": "pending", "verification_url": url,
                        "qr_data_uri": make_qr_data_uri(url), "error": None,
                    })
        await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] app init stream failed: %s", exc)
    finally:
        _APP_INIT_TASK = None
    # Completion check: does the shared HOME now have the app configuration
    aid = _app_configured()
    if aid:
        _app_init_state.update({
            "status": "configured", "app_id": aid,
            "verification_url": None, "qr_data_uri": None, "error": None,
        })
    else:
        _app_init_state.update({
            "status": "error", "verification_url": None, "qr_data_uri": None,
            "error": "应用配置未完成或已取消，请重试。",
        })


def _app_status_dict() -> Dict[str, Any]:
    aid = _app_configured()
    if aid:
        return {"configured": True, "app_id": aid, "status": "configured",
                "verification_url": None, "qr_data_uri": None, "error": None}
    return {"configured": False, "app_id": None,
            "status": _app_init_state.get("status", "idle"),
            "verification_url": _app_init_state.get("verification_url"),
            "qr_data_uri": _app_init_state.get("qr_data_uri"),
            "error": _app_init_state.get("error")}


class LarkService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = LarkConnectionRepository(db)

    def _fresh(self, user_id: str):
        """Discard the session cache and re-read — to see the latest state committed by the background task's independent session."""
        self.db.expire_all()
        return self.repo.ensure(user_id)

    # ── Status ───────────────────────────────────────────────────────────
    def status_dict(self, record) -> Dict[str, Any]:
        qr_target = record.login_verification_url_complete or record.login_verification_url
        return {
            "status": record.status,
            "lark_open_id": record.lark_open_id,
            "lark_name": record.lark_name,
            "tenant_key": record.tenant_key,
            "granted_scopes": list(record.granted_scopes or []),
            "verification_url": record.login_verification_url,
            "verification_url_complete": record.login_verification_url_complete,
            "qr_data_uri": make_qr_data_uri(qr_target) if record.status == "pending" else None,
            "user_code": record.login_user_code,
            "last_verified_at": record.last_verified_at.isoformat() if record.last_verified_at else None,
            "last_error": record.last_error,
        }

    def get_status(self, user_id: str) -> Dict[str, Any]:
        return self.status_dict(self.repo.ensure(user_id))

    async def probe_status(self, user_id: str) -> Dict[str, Any]:
        """Real-API liveness-probe reconciliation (corrects mismatches between the DB and actual credentials). Does not interrupt pending (login in progress)."""
        record = self._fresh(user_id)
        if record.status == "pending" and user_id in _LOGIN_TASKS:
            return self.status_dict(record)
        verdict = await verify_and_refresh(user_id)
        if verdict == "valid" and record.status != "connected":
            self.repo.update(user_id, {"status": "connected", "last_error": None})
        elif verdict == "invalid" and record.status == "connected":
            self.repo.update(user_id, {"status": "disconnected", "last_error": _EXPIRED_ERROR})
        return self.status_dict(self.repo.get(user_id))

    # ── Device-flow login (scan-to-bind) ─────────────────────────────────
    async def start_device_login(self, user_id: str) -> Dict[str, Any]:
        """Initiate the Lark device-flow login: first seed the app config, then
        ``auth login --no-wait`` gets the device_code / QR URL, and the
        background completion task is started. The front end presents
        qr_data_uri for the user to scan, then polls."""
        self.repo.ensure(user_id)
        # Cancel the same user's old task
        old = _LOGIN_TASKS.pop(user_id, None)
        if old is not None and not old.done():
            old.cancel()

        cfg_err = await _ensure_app_config(user_id)
        if cfg_err:
            self.repo.update(user_id, {"status": "error", **_CLEAR_LOGIN, "last_error": cfg_err})
            return self.status_dict(self.repo.get(user_id))

        out, err, rc = await _run_lark(user_id, _DEFAULT_LOGIN_ARGS, timeout=40)
        dev = parse_device_login(out)
        if rc != 0 or not (dev.get("verification_url") or dev.get("verification_url_complete")):
            tail = ((err or out or "").strip())[-200:]
            self.repo.update(user_id, {
                "status": "error", **_CLEAR_LOGIN,
                "last_error": f"发起扫码登录失败，请重试。{('详情: ' + tail) if tail else ''}",
            })
            return self.status_dict(self.repo.get(user_id))

        self.repo.update(user_id, {
            "status": "pending",
            "login_verification_url": dev.get("verification_url"),
            "login_verification_url_complete": dev.get("verification_url_complete"),
            "login_user_code": dev.get("user_code"),
            "login_device_code": dev.get("device_code"),
            "login_started_at": datetime.utcnow(),
            "last_error": None,
        })
        # Start the background completion task (blocking polling until authorized/expired)
        if dev.get("device_code"):
            _LOGIN_TASKS[user_id] = asyncio.create_task(
                _device_complete_flow(user_id, str(dev["device_code"]))
            )
        return self.status_dict(self._fresh(user_id))

    async def poll_login(self, user_id: str) -> Dict[str, Any]:
        """Poll login progress. The background task drives to connected/error;
        this reads the latest state. If the task is lost yet still pending
        (backend restart), fall back to the persisted device_code and run
        completion once more."""
        record = self._fresh(user_id)
        if record.status == "pending" and user_id not in _LOGIN_TASKS and record.login_device_code:
            _LOGIN_TASKS[user_id] = asyncio.create_task(
                _device_complete_flow(user_id, str(record.login_device_code))
            )
        return self.status_dict(record)

    # ── Disconnect ───────────────────────────────────────────────────────
    async def disconnect(self, user_id: str) -> Dict[str, Any]:
        """Cancel any in-progress login + backend logout + clear the backend persistent credential directory + set disconnected."""
        old = _LOGIN_TASKS.pop(user_id, None)
        if old is not None and not old.done():
            old.cancel()
        await _run_lark(user_id, ["auth", "logout"], timeout=30)
        try:
            from core.sandbox._common import lark_cache_dir, safe_user_id

            if safe_user_id(user_id):
                shutil.rmtree(lark_cache_dir(user_id), ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lark] disconnect cache rmtree failed user=%s: %s", user_id, exc)
        self.repo.update(
            user_id,
            {
                "status": "disconnected",
                "lark_open_id": None,
                "lark_name": None,
                "tenant_key": None,
                "granted_scopes": [],
                **_CLEAR_LOGIN,
                "auth_bundle": None,
                "last_error": None,
            },
        )
        return self.status_dict(self.repo.get(user_id))

    # ── Admin: shared-app one-click initialization (config init --new) ────
    def app_status(self) -> Dict[str, Any]:
        """Query the shared app's initialization state (configured / pending+QR code / error)."""
        return _app_status_dict()

    async def start_app_init(self) -> Dict[str, Any]:
        """Initiate ``config init --new``: run in the background and echo the configuration QR code for the admin to scan. If already configured, report back directly."""
        global _APP_INIT_TASK
        if _app_configured():
            return _app_status_dict()
        if _APP_INIT_TASK is not None and not _APP_INIT_TASK.done():
            return _app_status_dict()  # in progress, echo the current URL
        _app_init_state.update({"status": "pending", "verification_url": None,
                                "qr_data_uri": None, "error": None})
        _APP_INIT_TASK = asyncio.create_task(_app_init_flow())
        # Wait up to ~15s for the configuration URL to appear
        for _ in range(30):
            await asyncio.sleep(0.5)
            if _app_init_state.get("verification_url") or _app_init_state.get("status") in ("configured", "error"):
                break
        return _app_status_dict()

    async def reset_app(self) -> Dict[str, Any]:
        """Clear the shared app base (for re-initialization). Does not touch users' existing login tokens."""
        global _APP_INIT_TASK
        if _APP_INIT_TASK is not None and not _APP_INIT_TASK.done():
            _APP_INIT_TASK.cancel()
            _APP_INIT_TASK = None
        try:
            import shutil

            from core.sandbox._common import lark_app_home_dir

            shutil.rmtree(lark_app_home_dir(), ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lark] reset app home failed: %s", exc)
        _app_init_state.update({"status": "idle", "verification_url": None,
                                "qr_data_uri": None, "error": None})
        return _app_status_dict()
