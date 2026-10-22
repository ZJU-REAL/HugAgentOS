"""Integration-owned ephemeral sandbox credential handling."""
from __future__ import annotations
import base64
import logging
import time
from pathlib import Path
from typing import Any, Optional
logger = logging.getLogger(__name__)

class CubeCredentials:
    def __init__(self, request_timeout):
        self._request_timeout_s = request_timeout
        self._email_bootstrapped = set()

    async def ensure(self, sbx: Any, user_id: str) -> None:
        """Write this user's himalaya config.toml into this MicroVM, keyed by the **session-bound user_id** (one-time).

        Multi-tenant isolation crux, same as dws: credentials are only injected into
        the user's own session sandbox — never across users, never into warm/unbound
        sandboxes. Skipped when there is no connection / no credentials — himalaya
        commands then fail as unconfigured, and the email skill guides the user to
        bind a mailbox on the plugin detail page. cube has no bind-mount, so the
        config.toml file is written directly.
        """
        sid = getattr(sbx, "sandbox_id", None)
        if not sid or sid in self._email_bootstrapped:
            return
        self._email_bootstrapped.add(
            sid
        )  # register first, to avoid duplicate DB lookups under concurrency / when no credentials exist
        try:
            from integrations.email.service import get_email_config_bundle

            bundle = get_email_config_bundle(user_id)
            if not bundle:
                return
            import base64 as _b64

            content = _b64.b64decode(bundle).decode("utf-8")
            # The cube sandbox runs as root, HOME=/root → config lands at /root/.config/himalaya/config.toml
            await sbx.files.write("/root/.config/himalaya/config.toml", content)
            await sbx.commands.run(
                "chmod 600 /root/.config/himalaya/config.toml 2>/dev/null || true",
                user="root",
                timeout=30,
                request_timeout=30 + self._request_timeout_s,
            )
            logger.info("[cube] 已为 user=%s 注入邮箱凭据到 sandbox=%s", user_id, sid)
        except Exception as e:  # noqa: BLE001
            self._email_bootstrapped.discard(sid)
            logger.warning(
                "[cube] 注入邮箱凭据失败 user=%s sandbox=%s: %s", user_id, sid, str(e)[:160]
            )
