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
        self._lark_bootstrapped = set()

    def _build_lark_bundle_b64(self, user_id: str) -> Optional[str]:
        """Pack this user's file-based lark-cli credentials from the backend HOME into a base64 tar (None when no credentials).

        Selects the minimal credential set lark-cli recognizes on Linux (no OS
        keychain, all file-based):
        - ``.lark-cli/config.json``                      app config (app_id/brand)
        - ``.local/share/lark-cli/master.key``           local DEK
        - ``.local/share/lark-cli/appsecret_*.enc``      app secret ciphertext
        - ``.local/share/lark-cli/cli_*_ou_*.enc``       user token ciphertext (exists only after login)
        arcnames are relative to the HOME root, so in the sandbox ``tar -xf -C /root``
        restores the identical layout at ``/root/.lark-cli`` and
        ``/root/.local/share/lark-cli``. Missing config.json (app not seeded) → None.
        """
        import glob
        import io
        import tarfile

        from integrations.lark.storage import lark_home_dir

        home = lark_home_dir(user_id)
        cfg = home / ".lark-cli" / "config.json"
        if not cfg.exists():
            return None
        store = home / ".local" / "share" / "lark-cli"
        members: list[tuple[Path, str]] = [(cfg, ".lark-cli/config.json")]
        for name in ("master.key",):
            p = store / name
            if p.exists():
                members.append((p, f".local/share/lark-cli/{name}"))
        for pattern in ("appsecret_*.enc", "cli_*_ou_*.enc"):
            for path_str in glob.glob(str(store / pattern)):
                p = Path(path_str)
                members.append((p, f".local/share/lark-cli/{p.name}"))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for src, arcname in members:
                tar.add(str(src), arcname=arcname)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    async def ensure(self, sbx: Any, user_id: str) -> None:
        """Pack and write this user's lark-cli credentials into this MicroVM, keyed by the **session-bound user_id** (one-time).

        Multi-tenant isolation crux, same as dws/himalaya: credentials are only
        injected into the user's own session sandbox — never across users, never
        into warm/unbound sandboxes. Skipped when there is no app config / not
        logged in — lark-cli commands then fail as unconfigured/not-logged-in, and
        the lark-shared skill guides the user to complete auth login first. cube has
        no bind-mount, so it is packed as a tar and extracted directly into the
        sandbox root HOME (/root).
        """
        sid = getattr(sbx, "sandbox_id", None)
        if not sid or sid in self._lark_bootstrapped:
            return
        self._lark_bootstrapped.add(
            sid
        )  # register first, to avoid duplicate packing under concurrency / when no credentials exist
        try:
            bundle = self._build_lark_bundle_b64(user_id)
            if not bundle:
                return
            await sbx.files.write("/tmp/.lark-bundle.tar.b64", bundle)
            # The cube sandbox runs as root, HOME=/root → restores /root/.lark-cli and /root/.local/share/lark-cli
            await sbx.commands.run(
                "base64 -d /tmp/.lark-bundle.tar.b64 | tar -xf - -C /root && "
                "chmod -R go-rwx /root/.lark-cli /root/.local/share/lark-cli 2>/dev/null; "
                "rm -f /tmp/.lark-bundle.tar.b64",
                user="root",
                timeout=60,
                request_timeout=60 + self._request_timeout_s,
            )
            logger.info("[cube] 已为 user=%s 注入飞书凭据到 sandbox=%s", user_id, sid)
        except Exception as e:  # noqa: BLE001
            self._lark_bootstrapped.discard(sid)
            logger.warning(
                "[cube] 注入飞书凭据失败 user=%s sandbox=%s: %s", user_id, sid, str(e)[:160]
            )
