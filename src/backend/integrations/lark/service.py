"""Lark service responsibilities."""
from __future__ import annotations
from core.infra.time import utc_now
import asyncio
import shutil
from typing import Any, Dict
from core.db.threads import AsyncRepository
from integrations.lark.repository import LarkConnectionRepository
from integrations.dingtalk.parsing import make_qr_data_uri
import logging
logger = logging.getLogger(__name__)
from integrations.lark import configuration as lark_configuration
from integrations.lark import login as lark_login
from integrations.lark import parsing as lark_parsing
from integrations.lark import process as lark_process
from integrations.lark import state as lark_state

class LarkService:
    """Connection state lives in the repository; every access is its own short session."""

    def __init__(self):
        self.repo = AsyncRepository(LarkConnectionRepository)

    async def _fresh(self, user_id: str):
        """Discard the session cache and re-read — to see the latest state committed by the background task's independent session."""
        return await self.repo.ensure(user_id)

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

    async def get_status(self, user_id: str) -> Dict[str, Any]:
        return self.status_dict(await self.repo.ensure(user_id))

    async def probe_status(self, user_id: str) -> Dict[str, Any]:
        """Real-API liveness-probe reconciliation (corrects mismatches between the DB and actual credentials). Does not interrupt pending (login in progress)."""
        record = await self._fresh(user_id)
        if record.status == "pending" and user_id in lark_state.LOGIN_TASKS:
            return self.status_dict(record)
        verdict = await lark_login.verify_and_refresh(user_id)
        if verdict == "valid" and record.status != "connected":
            await self.repo.update(user_id, {"status": "connected", "last_error": None})
        elif verdict == "invalid" and record.status == "connected":
            await self.repo.update(user_id, {"status": "disconnected", "last_error": lark_state.EXPIRED_ERROR})
        return self.status_dict(await self.repo.get(user_id))

    # ── Device-flow login (scan-to-bind) ─────────────────────────────────
    async def start_device_login(self, user_id: str) -> Dict[str, Any]:
        """Initiate the Lark device-flow login: first seed the app config, then
        ``auth login --no-wait`` gets the device_code / QR URL, and the
        background completion task is started. The front end presents
        qr_data_uri for the user to scan, then polls."""
        await self.repo.ensure(user_id)
        # Cancel the same user's old task
        old = lark_state.LOGIN_TASKS.pop(user_id, None)
        if old is not None and not old.done():
            old.cancel()

        cfg_err = await lark_configuration.ensure_app_config(user_id)
        if cfg_err:
            await self.repo.update(user_id, {"status": "error", **lark_state.CLEAR_LOGIN, "last_error": cfg_err})
            return self.status_dict(await self.repo.get(user_id))

        out, err, rc = await lark_process.run_lark(user_id, lark_state.DEFAULT_LOGIN_ARGS, timeout=40)
        dev = lark_parsing.parse_device_login(out)
        if rc != 0 or not (dev.get("verification_url") or dev.get("verification_url_complete")):
            tail = ((err or out or "").strip())[-200:]
            await self.repo.update(user_id, {
                "status": "error", **lark_state.CLEAR_LOGIN,
                "last_error": f"发起扫码登录失败，请重试。{('详情: ' + tail) if tail else ''}",
            })
            return self.status_dict(await self.repo.get(user_id))

        await self.repo.update(user_id, {
            "status": "pending",
            "login_verification_url": dev.get("verification_url"),
            "login_verification_url_complete": dev.get("verification_url_complete"),
            "login_user_code": dev.get("user_code"),
            "login_device_code": dev.get("device_code"),
            "login_started_at": utc_now(),
            "last_error": None,
        })
        # Start the background completion task (blocking polling until authorized/expired)
        if dev.get("device_code"):
            lark_state.LOGIN_TASKS[user_id] = asyncio.create_task(
                lark_login.device_complete_flow(user_id, str(dev["device_code"]))
            )
        return self.status_dict(await self._fresh(user_id))

    async def poll_login(self, user_id: str) -> Dict[str, Any]:
        """Poll login progress. The background task drives to connected/error;
        this reads the latest state. If the task is lost yet still pending
        (backend restart), fall back to the persisted device_code and run
        completion once more."""
        record = await self._fresh(user_id)
        if record.status == "pending" and user_id not in lark_state.LOGIN_TASKS and record.login_device_code:
            lark_state.LOGIN_TASKS[user_id] = asyncio.create_task(
                lark_login.device_complete_flow(user_id, str(record.login_device_code))
            )
        return self.status_dict(record)

    # ── Disconnect ───────────────────────────────────────────────────────
    async def disconnect(self, user_id: str) -> Dict[str, Any]:
        """Cancel any in-progress login + backend logout + clear the backend persistent credential directory + set disconnected."""
        old = lark_state.LOGIN_TASKS.pop(user_id, None)
        if old is not None and not old.done():
            old.cancel()
        await lark_process.run_lark(user_id, ["auth", "logout"], timeout=30)
        try:
            from integrations.lark.storage import lark_cache_dir
            from core.sandbox.workspace.credentials import purge_credential_dir
            from core.sandbox.workspace.identity import safe_user_id

            # Empty in place — never rmtree the root. ~/.lark-cli and
            # ~/.local/share/lark-cli under it are bind-mounted into this user's
            # live sandboxes; removing the directory orphans those mounts and the
            # next login's credentials land on an inode the sandbox cannot see
            # (see [[purge_credential_dir]]).
            if safe_user_id(user_id):
                purge_credential_dir(lark_cache_dir(user_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lark] disconnect cache purge failed user=%s: %s", user_id, exc)
        await self.repo.update(
            user_id,
            {
                "status": "disconnected",
                "lark_open_id": None,
                "lark_name": None,
                "tenant_key": None,
                "granted_scopes": [],
                **lark_state.CLEAR_LOGIN,
                "auth_bundle": None,
                "last_error": None,
            },
        )
        return self.status_dict(await self.repo.get(user_id))

    # ── Admin: shared-app one-click initialization (config init --new) ────
    def app_status(self) -> Dict[str, Any]:
        """Query the shared app's initialization state (configured / pending+QR code / error)."""
        return lark_configuration.app_status_dict()

    async def start_app_init(self) -> Dict[str, Any]:
        """Initiate ``config init --new``: run in the background and echo the configuration QR code for the admin to scan. If already configured, report back directly."""
        pass  # Shared task state is owned by the state module.
        if lark_configuration.app_configured():
            return lark_configuration.app_status_dict()
        if lark_state.APP_INIT_TASK is not None and not lark_state.APP_INIT_TASK.done():
            return lark_configuration.app_status_dict()  # in progress, echo the current URL
        lark_state.app_init_state.update({"status": "pending", "verification_url": None,
                                "qr_data_uri": None, "error": None})
        lark_state.APP_INIT_TASK = asyncio.create_task(lark_configuration.app_init_flow())
        # Wait up to ~15s for the configuration URL to appear
        for _ in range(30):
            await asyncio.sleep(0.5)
            if lark_state.app_init_state.get("verification_url") or lark_state.app_init_state.get("status") in ("configured", "error"):
                break
        return lark_configuration.app_status_dict()

    async def reset_app(self) -> Dict[str, Any]:
        """Clear the shared app base (for re-initialization). Does not touch users' existing login tokens."""
        pass  # Shared task state is owned by the state module.
        if lark_state.APP_INIT_TASK is not None and not lark_state.APP_INIT_TASK.done():
            lark_state.APP_INIT_TASK.cancel()
            lark_state.APP_INIT_TASK = None
        try:
            import shutil

            from integrations.lark.storage import lark_app_home_dir

            shutil.rmtree(lark_app_home_dir(), ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lark] reset app home failed: %s", exc)
        lark_state.app_init_state.update({"status": "idle", "verification_url": None,
                                "qr_data_uri": None, "error": None})
        return lark_configuration.app_status_dict()
