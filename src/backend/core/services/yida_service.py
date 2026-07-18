"""Yida (openyida CLI) account connection service: backend orchestration for the "Connect Yida" panel on the plugin detail page.

Key difference from the dingtalk/lark services: openyida is a Node CLI, and the
backend slim image **does not install Node** (same decision as choosing a static
Go binary for lark) — login commands are executed **inside that user's sandbox**:
the sandbox image has openyida preinstalled, and the fixed working directory
/home/ubuntu/yida-workspace is exactly where the login state is persisted:

- opensandbox: per-user bind-mount → cookies land directly on the host at ``yida_cache/{uid}/workspace``;
- cube: execute's openyida inject/write-back hooks apply automatically (cube_provider._persist_yida_state);
- script_runner: shared working directory (one per deployment, see _common.yida_shared_workspace_dir).

Three-stage login (openyida agent native contract, see its bin/yida.js login branch):

1. ``openyida login --agent-qr``
   → ``{status:"need_qr_scan", qr_url, session_file, poll_command, ...}``
2. ``openyida login --agent-poll <session_file>``
   → ``{status:"ok", corp_id, user_id, base_url}`` (login complete, cookies written to the workspace)
   → or ``{status:"need_corp_selection", organizations:[{corp_id,corp_name,main_org}]}``
3. Multi-org: ``openyida login --agent-select <session_file> --corp-id <X>`` → ``{status:"ok"}``

The QR code is rendered by the backend itself as a data URI from ``qr_url``
(the CLI's qr_image_markdown points at a PNG local to the sandbox, unusable by
the panel). **No DB table**: connection status uses the host-side cookie files
as the source of truth; identity metadata lands in
``yida_cache/{uid}/connection.json``; in-flight QR-scan sessions live in
process memory (with a TTL). In-chat QR login (the yida skill) writes the same
cookie file as this panel, so the two paths interoperate.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from core.config.settings import settings
from core.sandbox._common import safe_user_id, yida_shared_workspace_dir, yida_workspace_dir
from core.services.dingtalk_service import make_qr_data_uri

logger = logging.getLogger(__name__)

# Fixed working directory inside the sandbox (matches
# _opensandbox_internals._YIDA_WORKSPACE_MOUNT / SKILL.md; not imported directly
# to avoid pulling in the opensandbox dependency chain — a test keeps the two in sync)
YIDA_WORKSPACE_MOUNT = "/home/ubuntu/yida-workspace"

# session_file / corp_id only allow a conservative character set, eliminating the injection surface when spliced into shell commands
_SESSION_FILE_RE = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
_CORP_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# In-flight QR-scan sessions: user_id -> {session_file, qr_url, started_at}.
# Process memory is enough — the QR code itself expires within minutes, and
# losing sessions on a backend restart just makes the user click "connect"
# once more; a negligible cost.
_PENDING: Dict[str, Dict[str, Any]] = {}
_PENDING_TTL_S = 10 * 60


def extract_result_json(stdout: str) -> Optional[Dict[str, Any]]:
    """Extract the result JSON from CLI stdout. The agent-family commands emit a
    single-line ``JSON.stringify(result)``, but stray sandbox/CLI lines may
    surround it — scan from the last line backwards for the first line that
    parses into a dict."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _host_workspace_dir(user_id: str) -> Path:
    """Host source-of-truth path for this user's Yida workspace (where the cookie files live).

    script_runner is a shared container (the provider ignores user_id), so the
    login state is one per deployment → shared directory; the other providers
    (opensandbox / cube) are per-user.
    """
    if (settings.sandbox.provider or "").strip() == "script_runner":
        return yida_shared_workspace_dir()
    return yida_workspace_dir(user_id)


def _cookie_files(ws: Path) -> list[Path]:
    return sorted((ws / ".cache").glob("cookies*.json")) if (ws / ".cache").is_dir() else []


def _connection_meta_path(user_id: str) -> Path:
    return _host_workspace_dir(user_id).parent / "connection.json"


def _load_meta(user_id: str) -> Dict[str, Any]:
    try:
        return json.loads(_connection_meta_path(user_id).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_meta(user_id: str, meta: Dict[str, Any]) -> None:
    try:
        p = _connection_meta_path(user_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[yida] save connection meta failed user=%s: %s", user_id, exc)


def _get_pending(user_id: str) -> Optional[Dict[str, Any]]:
    p = _PENDING.get(user_id)
    if p and time.monotonic() - p["started_at"] > _PENDING_TTL_S:
        _PENDING.pop(user_id, None)
        return None
    return p


def _cookies_written_since(user_id: str, pending: Dict[str, Any]) -> bool:
    """Whether host-side cookies have been written since this QR session started (= login actually completed).

    Covers two timing cases where login "succeeded but poll never sees ok": the
    CLI deletes the session file on success (subsequent agent-poll calls all
    ENOENT); and login completed via the other path (in-chat skill QR scan).
    """
    age_budget = time.monotonic() - pending["started_at"] + 5  # 5s clock slack
    for f in _cookie_files(_host_workspace_dir(user_id)):
        try:
            if time.time() - f.stat().st_mtime <= age_budget:
                return True
        except OSError:
            continue
    return False


# Stale-sandbox probe marker: the command echoes this marker when the working
# directory is missing (sandbox created before the yida volume, no mount)
_NO_WS_MARKER = "__JX_YIDA_NO_WS__"


class YidaService:
    """No DB dependency (the cookie files are the source of truth for connection status); the constructor takes no args to match route-layer usage."""

    async def _run_in_sandbox(self, user_id: str, sub_command: str, timeout: int) -> tuple[str, int]:
        """Run a command in the Yida workspace of this user's **persistent** sandbox; returns (stdout, exit_code).

        A synthetic session_id (``yida-connect-{uid}``) is mandatory:
        opensandbox routes session-less requests to the light ephemeral pool —
        those sandboxes mount no per-user volumes, ``/home/ubuntu/yida-workspace``
        does not exist, and login cookies have nowhere to persist. Only with a
        session_id does the request route to the user-bound persistent sandbox.

        Self-healing: the user's existing sandbox may predate the yida volume /
        new image (adopted and reused after a backend restart), manifesting as a
        missing working directory (cd fails, echoing _NO_WS_MARKER) or a missing
        openyida (rc 127). On detection, ``close_session`` genuinely destroys it
        and we retry, rebuilding at most twice (the user's pool may hoard several
        stale idle sandboxes); if it still fails, log stderr and return as-is for
        the caller to report the error.
        """
        from core.sandbox.factory import get_sandbox_provider
        from core.sandbox.protocol import ExecuteRequest

        provider = get_sandbox_provider()
        session_id = f"yida-connect-{user_id}"
        guarded = (
            f"cd {YIDA_WORKSPACE_MOUNT} 2>/dev/null "
            f"|| {{ echo {_NO_WS_MARKER}; exit 97; }}; {sub_command}"
        )
        req = ExecuteRequest(
            script_content=guarded,
            script_name="_yida_connect.sh",
            language="bash",
            timeout=timeout,
            user_id=user_id,
            session_id=session_id,
        )
        for attempt in range(3):
            result = await provider.execute(req)
            out = result.stdout or ""
            err = getattr(result, "stderr", "") or ""
            rc = int(result.exit_code or 0)
            stale = _NO_WS_MARKER in out or rc == 127 or "command not found" in err
            if stale and attempt < 2:
                logger.info(
                    "[yida] 检测到陈旧沙箱（无工作目录挂载或无 openyida），销毁重建 "
                    "user=%s attempt=%d", user_id, attempt + 1,
                )
                try:
                    await provider.close_session(session_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[yida] close_session 失败 user=%s: %s", user_id, exc)
                continue
            if rc != 0:
                logger.info(
                    "[yida] 沙箱命令非零退出 user=%s rc=%s stderr=%.300s", user_id, rc, err,
                )
            return out, rc
        return "", 97  # theoretically unreachable (the loop always returns); a fuse

    # ── Status ──────────────────────────────────────────────────────────

    def get_status(self, user_id: str) -> Dict[str, Any]:
        uid = safe_user_id(user_id)
        if not uid:
            return {"status": "disconnected"}
        pending = _get_pending(uid)
        if pending:
            # Cookies written during the session = login actually completed
            # (possibly via a timing the poll summary didn't recognize, or an
            # in-chat scan); clear pending and fall back to connected instead
            # of staying stuck in the scanning state.
            if _cookies_written_since(uid, pending):
                _PENDING.pop(uid, None)
            else:
                return self._pending_response(pending)
        if _cookie_files(_host_workspace_dir(uid)):
            meta = _load_meta(uid)
            return {
                "status": "connected",
                "corp_id": meta.get("corp_id"),
                "yida_user_id": meta.get("user_id"),
                "base_url": meta.get("base_url"),
                "connected_at": meta.get("connected_at"),
            }
        return {"status": "disconnected"}

    # ── Three-stage login ───────────────────────────────────────────────

    async def start_login(self, user_id: str) -> Dict[str, Any]:
        uid = safe_user_id(user_id)
        if not uid:
            return {"status": "error", "error": "invalid user"}
        stdout, rc = await self._run_in_sandbox(uid, "openyida login --agent-qr", timeout=90)
        data = extract_result_json(stdout)
        if not data or data.get("status") != "need_qr_scan":
            logger.warning("[yida] agent-qr 无法解析 user=%s rc=%s out=%.200s", uid, rc, stdout)
            return {"status": "error", "error": "启动扫码登录失败，请稍后重试"}
        session_file = str(data.get("session_file") or "")
        if not _SESSION_FILE_RE.match(session_file):
            logger.warning("[yida] session_file 非法 user=%s: %.100s", uid, session_file)
            return {"status": "error", "error": "登录会话异常，请重试"}
        qr_url = str(data.get("qr_url") or "")
        _PENDING[uid] = {
            "session_file": session_file,
            "qr_url": qr_url,
            "started_at": time.monotonic(),
        }
        return {
            "status": "pending",
            "qr_data_uri": make_qr_data_uri(qr_url),
            "qr_url": qr_url,
            "message": "请用钉钉 App 扫码并确认登录",
        }

    async def poll_login(self, user_id: str, corp_id: Optional[str] = None) -> Dict[str, Any]:
        uid = safe_user_id(user_id)
        pending = _get_pending(uid) if uid else None
        if not pending:
            # No in-flight session: return the current state (may already have completed via in-chat scan)
            return self.get_status(uid or "")
        if corp_id and not _CORP_ID_RE.match(corp_id):
            return {"status": "error", "error": "corp_id 非法"}
        sf = pending["session_file"]
        # Multi-org stage two: with corp_id go through --agent-select; otherwise --agent-poll waits for the scan
        if corp_id and pending.get("corp_selection"):
            sub = f"openyida login --agent-select {sf} --corp-id {corp_id}"
        elif corp_id:
            sub = f"openyida login --agent-poll {sf} --corp-id {corp_id}"
        else:
            sub = f"openyida login --agent-poll {sf}"
        stdout, rc = await self._run_in_sandbox(uid, sub, timeout=45)
        data = extract_result_json(stdout)
        if not data:
            # Not scanned / timed out / session file already cleaned up by the CLI
            # on success (ENOENT): first check whether cookies were written during
            # the session — if so, login actually completed; don't leave the
            # frontend spinning forever.
            if _cookies_written_since(uid, pending):
                _PENDING.pop(uid, None)
                meta = {**_load_meta(uid), "connected_at": int(time.time())}
                _save_meta(uid, meta)
                logger.info("[yida] 登录完成（cookie 落盘兜底判定）user=%s", uid)
                return self.get_status(uid)
            return self._pending_response(pending)
        status = data.get("status")
        # Success comes in two shapes: the full result carries status:"ok"; the
        # CLI printLoginResult summary is only {"ok":true, corp_id, user_id,
        # base_url, ...} **without a status field**.
        if status == "ok" or (status is None and data.get("ok") is True):
            _PENDING.pop(uid, None)
            meta = {
                "corp_id": data.get("corp_id"),
                "user_id": data.get("user_id"),
                "base_url": data.get("base_url"),
                "connected_at": int(time.time()),
            }
            _save_meta(uid, meta)
            logger.info("[yida] 登录完成 user=%s corp=%s", uid, meta["corp_id"])
            return {"status": "connected", **{k: meta[k] for k in ("corp_id", "base_url")}}
        if status == "need_corp_selection":
            pending["corp_selection"] = True
            orgs = data.get("organizations") or []
            return {
                "status": "corp_selection",
                "organizations": [
                    {
                        "corp_id": o.get("corp_id"),
                        "corp_name": o.get("corp_name"),
                        "main_org": bool(o.get("main_org")),
                    }
                    for o in orgs
                    if isinstance(o, dict)
                ],
            }
        logger.info("[yida] poll 未完成 user=%s status=%s rc=%s", uid, status, rc)
        return self._pending_response(pending)

    @staticmethod
    def _pending_response(pending: Dict[str, Any]) -> Dict[str, Any]:
        """A pending response must carry the QR code: the frontend overwrites the
        status wholesale, so if poll's pending lacks qr_data_uri it wipes the QR
        code being displayed (in the initial release this is exactly how it got
        stuck on the "fetching login QR code..." screen, observed in practice)."""
        return {
            "status": "pending",
            "qr_data_uri": make_qr_data_uri(pending.get("qr_url")),
            "qr_url": pending.get("qr_url"),
        }

    # ── Disconnect ──────────────────────────────────────────────────────

    async def disconnect(self, user_id: str) -> Dict[str, Any]:
        uid = safe_user_id(user_id)
        if not uid:
            return {"status": "disconnected"}
        _PENDING.pop(uid, None)
        # Host source of truth: delete cookies + metadata (for opensandbox/script-runner this is the same file the sandbox sees)
        ws = _host_workspace_dir(uid)
        for f in _cookie_files(ws):
            try:
                f.unlink()
            except OSError as exc:
                logger.warning("[yida] 删 cookie 失败 %s: %s", f, exc)
        try:
            _connection_meta_path(uid).unlink(missing_ok=True)
        except OSError:
            pass
        # Best-effort cleanup of the sandbox-side copy (the copy injected by cube does not disappear automatically when the host copy is deleted)
        try:
            await self._run_in_sandbox(uid, "rm -f .cache/cookies*.json || true", timeout=30)
        except Exception as exc:  # noqa: BLE001
            logger.info("[yida] 清沙箱 cookie 副本失败（忽略）user=%s: %s", uid, exc)
        return {"status": "disconnected"}
