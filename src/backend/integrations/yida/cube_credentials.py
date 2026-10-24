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
        self._yida_bootstrapped = set()

    _YIDA_STATE_GLOB = ".cache/*.json"
    _YIDA_STATE_MAX_B64 = 1024 * 1024

    def _build_yida_state_b64(self, user_id: str) -> Optional[str]:
        """Pack ``.cache/*.json`` from this user's host-side Yida working directory into a base64 tar (None when absent)."""
        import glob
        import io
        import tarfile

        from integrations.yida.storage import yida_workspace_dir

        ws = yida_workspace_dir(user_id)
        paths = sorted(glob.glob(str(ws / ".cache" / "*.json")))
        if not paths:
            return None
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for path_str in paths:
                p = Path(path_str)
                tar.add(str(p), arcname=f".cache/{p.name}")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    async def ensure(self, sbx: Any, user_id: str) -> None:
        """Inject this user's Yida login state into this MicroVM, keyed by the **session-bound user_id** (one-time).

        Multi-tenant isolation crux, same as dws/himalaya/lark: inject only into the
        user's own session sandbox — never across users, never into warm/unbound
        sandboxes. The fixed working directory /home/ubuntu/yida-workspace is
        **created up front regardless of whether login state exists** (the yida
        skill force-cds into it; a missing directory would make every command fail
        outright); when the host side has no cookies, only the directory is created —
        openyida commands fail as not-logged-in, and the skill presents a QR code to
        scan within the conversation.
        """
        sid = getattr(sbx, "sandbox_id", None)
        if not sid or sid in self._yida_bootstrapped:
            return
        self._yida_bootstrapped.add(
            sid
        )  # register first, to avoid duplicate packing under concurrency / when no credentials exist
        try:
            from integrations.yida.storage import YIDA_WORKSPACE_MOUNT as _YIDA_WORKSPACE_MOUNT

            bundle = self._build_yida_state_b64(user_id)
            if not bundle:
                await sbx.commands.run(
                    f"mkdir -p {_YIDA_WORKSPACE_MOUNT}",
                    user="root",
                    timeout=30,
                    request_timeout=30 + self._request_timeout_s,
                )
                return
            await sbx.files.write("/tmp/.yida-state.tar.b64", bundle)
            await sbx.commands.run(
                f"mkdir -p {_YIDA_WORKSPACE_MOUNT} && "
                f"base64 -d /tmp/.yida-state.tar.b64 | tar -xf - -C {_YIDA_WORKSPACE_MOUNT} && "
                f"chmod -R go-rwx {_YIDA_WORKSPACE_MOUNT}/.cache 2>/dev/null; "
                "rm -f /tmp/.yida-state.tar.b64",
                user="root",
                timeout=60,
                request_timeout=60 + self._request_timeout_s,
            )
            logger.info("[cube] 已为 user=%s 注入宜搭登录态到 sandbox=%s", user_id, sid)
        except Exception as e:  # noqa: BLE001
            self._yida_bootstrapped.discard(sid)
            logger.warning(
                "[cube] 注入宜搭登录态失败 user=%s sandbox=%s: %s", user_id, sid, str(e)[:160]
            )

    @staticmethod
    def extract_state(raw_b64: str, dest_ws: Path) -> int:
        """Safely extract the base64 tar sent back from the sandbox into the host working directory; returns the number of files written.

        Accepts only regular-file members of the form ``.cache/<basename>.json``
        (guarding against path traversal / symlinks / smuggled subdirectories),
        writing each member to disk manually instead of extractall. Kept as a
        standalone static method for easy unit testing.
        """
        import io
        import tarfile

        data = base64.b64decode(raw_b64)
        count = 0
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            for m in tar.getmembers():
                name = m.name
                while name.startswith("./"):
                    name = name[2:]
                base = name[len(".cache/") :]
                if (
                    not m.isreg()
                    or not name.startswith(".cache/")
                    or "/" in base
                    or not base.endswith(".json")
                ):
                    continue
                f = tar.extractfile(m)
                if f is None:
                    continue
                target = dest_ws / ".cache" / base
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f.read())
                try:
                    target.chmod(0o600)
                except OSError:
                    pass
                count += 1
        return count

    async def persist(self, sbx: Any, user_id: str) -> None:
        """After an openyida command runs, send the in-sandbox Yida login state back to the host and persist it (best-effort).

        openyida's QR-code login and headless CSRF-token refresh both happen inside
        the sandbox, writing ``.cache/*.json`` in the working directory — cube has
        no bind-mount, so without sending it back the login state is lost the moment
        the sandbox (TTL on the minutes scale) is destroyed, and the user has to
        rescan the QR code with every new sandbox. Failures are only logged and
        never affect the command result.
        """
        try:
            from integrations.yida.storage import YIDA_WORKSPACE_MOUNT as _YIDA_WORKSPACE_MOUNT, yida_workspace_dir

            res = await sbx.commands.run(
                f"cd {_YIDA_WORKSPACE_MOUNT} 2>/dev/null && "
                f"ls {self._YIDA_STATE_GLOB} >/dev/null 2>&1 && "
                f"tar -cf - {self._YIDA_STATE_GLOB} | base64 -w0 || true",
                user="root",
                timeout=30,
                request_timeout=30 + self._request_timeout_s,
            )
            raw = (getattr(res, "stdout", "") or "").strip()
            if not raw or len(raw) > self._YIDA_STATE_MAX_B64:
                return
            n = self.extract_state(raw, yida_workspace_dir(user_id))
            if n:
                logger.debug("[cube] 已回传宜搭登录态 user=%s files=%d", user_id, n)
        except Exception as e:  # noqa: BLE001
            logger.warning("[cube] 回传宜搭登录态失败 user=%s: %s", user_id, str(e)[:160])
