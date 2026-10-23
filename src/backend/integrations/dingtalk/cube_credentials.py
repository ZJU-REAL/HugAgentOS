"""Integration-owned ephemeral sandbox credential handling."""
from __future__ import annotations
import base64
import logging
import time
from pathlib import Path
from typing import Any, Optional
from core.db.threads import run_in_session
logger = logging.getLogger(__name__)

class CubeCredentials:
    def __init__(self, request_timeout):
        self._request_timeout_s = request_timeout
        self._dingtalk_bootstrapped = set()
        self._dingtalk_bundle_cache = {}

    _DINGTALK_BUNDLE_TTL_S = 30.0

    async def _export_live_dingtalk_bundle(self, user_id: str) -> Optional[str]:
        """Run ``dws auth export --base64`` live against this user's backend-resident HOME to get a live credential bundle.

        Call precondition: this method is only called by :meth:`_get_dingtalk_bundle`
        after ``verify_and_refresh`` has ruled out ``invalid`` — at that point the
        access token in the backend HOME is freshly refreshed, so the exported bundle
        is a **currently valid** credential. (``dws auth export`` itself does not
        trigger a refresh, so the verify step's real request must renew the token
        first; otherwise, once access expires, the export would be an expired
        ticket.) Carries a ~30s short cache: when multiple session sandboxes are
        created almost simultaneously, only one export is forked. Returns None on
        export failure; the caller decides the fallback based on the verdict.
        """
        now = time.monotonic()
        cached = self._dingtalk_bundle_cache.get(user_id)
        if cached and now - cached[1] < self._DINGTALK_BUNDLE_TTL_S:
            return cached[0]
        try:
            from integrations.dingtalk.service import _run_dws

            out, _, rc = await _run_dws(user_id, ["auth", "export", "--base64"], timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.info("[cube] 现导出钉钉凭据失败 user=%s: %s", user_id, str(e)[:120])
            return None
        bundle = out.strip() if rc == 0 else ""
        if not bundle:
            return None
        self._dingtalk_bundle_cache[user_id] = (bundle, now)
        return bundle

    async def _get_dingtalk_bundle(self, user_id: str) -> Optional[str]:
        """The user's portable DingTalk credential bundle (base64), if connected. Returned only when status=connected AND **server-side verification passes**.

        Before injection, :func:`verify_and_refresh` makes one real request to
        validate the backend HOME credentials:
          - ``invalid`` (refresh token already rotated/expired) → persist
            disconnected to the DB + prompt to reconnect; **do not inject dead
            credentials** for the sandbox to bang its head against;
          - ``valid`` → that verification has already made the **backend HOME
            refresh + rotate in place** (single holder, eliminating multi-copy
            rotation races); the access token is a freshly rotated ~2h ticket, then
            exported live and injected — during the session (minutes-scale) the
            sandbox calls with this valid access token directly, triggering no
            refresh and no rotation, so the backend's refresh token is not
            invalidated by the sandbox copy;
          - ``unknown`` (network/rate-limit, inconclusive) → leave the DB untouched,
            best-effort live export, fall back to the frozen snapshot only if the
            export fails.
        """
        try:
            from integrations.dingtalk.repository import DingTalkConnectionRepository
            from integrations.dingtalk.service import mark_login_expired, verify_and_refresh

            def _connection(db):
                rec = DingTalkConnectionRepository(db).get(user_id)
                return rec
            rec = await run_in_session(_connection)
            if not rec or rec.status != "connected":
                return None
            verdict = await verify_and_refresh(user_id)
            if verdict == "invalid":
                mark_login_expired(user_id)
                logger.info("[cube] 钉钉凭据已失效，置 disconnected user=%s", user_id)
                return None
            fresh = await self._export_live_dingtalk_bundle(user_id)
            if fresh:
                return fresh
            # Export failed: for valid we must not fall back to the necessarily older frozen snapshot; only unknown gets the fallback
            return (rec.auth_bundle or None) if verdict == "unknown" else None
        except Exception as e:  # noqa: BLE001
            logger.warning("[cube] 读钉钉凭据包失败 user=%s: %s", user_id, str(e)[:120])
            return None

    async def ensure(self, sbx: Any, user_id: str) -> None:
        """Import this user's dws credential bundle into this MicroVM, keyed by the **session-bound user_id** (one-time).

        Multi-tenant isolation crux: credentials are only injected into the user's
        own session sandbox — never across users, never into warm/unbound sandboxes
        (this method is called only after execute has obtained the session's sandbox
        and the command contains dws). Skipped when there is no connection / no
        credential bundle — dws commands then fail as not-logged-in, and the skill
        guides the user to connect on the settings page.
        """
        sid = getattr(sbx, "sandbox_id", None)
        if not sid or sid in self._dingtalk_bootstrapped:
            return
        self._dingtalk_bootstrapped.add(
            sid
        )  # register first, to avoid duplicate DB lookups under concurrency / when no credentials exist
        bundle = await self._get_dingtalk_bundle(user_id)
        if not bundle:
            return
        try:
            from integrations.dingtalk.environment import dws_extra_envs

            await sbx.files.write("/tmp/.dws-bundle.b64", bundle)
            await sbx.commands.run(
                "dws auth import --base64 -i /tmp/.dws-bundle.b64 --force "
                "2>/dev/null; rm -f /tmp/.dws-bundle.b64",
                user="root",
                envs=dws_extra_envs() or None,
                timeout=60,
                request_timeout=60 + self._request_timeout_s,
            )
            logger.info("[cube] 已为 user=%s 注入钉钉凭据到 sandbox=%s", user_id, sid)
        except Exception as e:  # noqa: BLE001
            # Injection failure does not block this command (dws will naturally report not-logged-in); the next sandbox will retry.
            self._dingtalk_bootstrapped.discard(sid)
            logger.warning(
                "[cube] 注入钉钉凭据失败 user=%s sandbox=%s: %s", user_id, sid, str(e)[:160]
            )
