"""Email account connection service (email plugin / himalaya CLI).

Binds a mailbox **as the current user** with per-user IMAP/SMTP + an app
password, persists connection status / account metadata to the
``email_connections`` table, and stores the app password Fernet-encrypted in
``secret_enc``. The credential proper (himalaya ``config.toml``) is written to
the backend persistent volume ``$STORAGE/email_cache/{uid}/home/.config/
himalaya/config.toml`` (0600), bind-mounted into the sandbox himalaya's
``~/.config/himalaya``.

Biggest difference from dingtalk/lark: **no device flow, no OAuth, no QR code,
no background jobs**. Binding is **synchronous** — "save form → render
config.toml → ``himalaya folder list`` connectivity check": success means
connected, failure means error. So the state machine is only
disconnected/connected/error (no pending).

himalaya config/command shapes follow v1.2.0 (see
internal design docs): config uses
``[accounts.default]`` + ``backend.*`` (IMAP receive) +
``message.send.backend.*`` (SMTP send);
encryption.type ∈ {tls, start-tls, none}; the app password goes in ``backend.auth.raw``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.db.repository import EmailConnectionRepository
from core.infra.crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

_HIMALAYA_BIN = "himalaya"
# Fixed single-account name "default" inside the sandbox; himalaya commands in skills act on it without -a.
_ACCOUNT = "default"

# Fallback message shown to the frontend on verification failure (the concrete stderr summary is appended after it).
_VERIFY_FAIL = "邮箱连接校验失败，请检查邮箱地址、授权码和服务器设置后重试"


# ── Default IMAP/SMTP for common providers (convenience prefill, not credentials; user can override) ──
# Value: (provider, imap_host, imap_port, imap_security, smtp_host, smtp_port, smtp_security)
# security ∈ {tls, starttls, none} (externally / to the frontend use starttls; mapped to start-tls when rendering the config).
_PROVIDERS: Dict[str, Tuple[str, str, int, str, str, int, str]] = {
    "gmail.com":        ("gmail",    "imap.gmail.com",        993, "tls", "smtp.gmail.com",        465, "tls"),
    "googlemail.com":   ("gmail",    "imap.gmail.com",        993, "tls", "smtp.gmail.com",        465, "tls"),
    "outlook.com":      ("outlook",  "outlook.office365.com", 993, "tls", "smtp.office365.com",    587, "starttls"),
    "hotmail.com":      ("outlook",  "outlook.office365.com", 993, "tls", "smtp.office365.com",    587, "starttls"),
    "live.com":         ("outlook",  "outlook.office365.com", 993, "tls", "smtp.office365.com",    587, "starttls"),
    "office365.com":    ("outlook",  "outlook.office365.com", 993, "tls", "smtp.office365.com",    587, "starttls"),
    "163.com":          ("netease",  "imap.163.com",          993, "tls", "smtp.163.com",          465, "tls"),
    "126.com":          ("netease",  "imap.126.com",          993, "tls", "smtp.126.com",          465, "tls"),
    "yeah.net":         ("netease",  "imap.yeah.net",         993, "tls", "smtp.yeah.net",         465, "tls"),
    "qq.com":           ("qq",       "imap.qq.com",           993, "tls", "smtp.qq.com",           465, "tls"),
    "foxmail.com":      ("qq",       "imap.qq.com",           993, "tls", "smtp.qq.com",           465, "tls"),
    "qiye.163.com":     ("qiye163",  "imap.qiye.163.com",     993, "tls", "smtp.qiye.163.com",     465, "tls"),
    "exmail.qq.com":    ("exmail",   "imap.exmail.qq.com",    993, "tls", "smtp.exmail.qq.com",    465, "tls"),
    "sina.com":         ("sina",     "imap.sina.com",         993, "tls", "smtp.sina.com",         465, "tls"),
    "aliyun.com":       ("aliyun",   "imap.aliyun.com",       993, "tls", "smtp.aliyun.com",       465, "tls"),
}


def detect_provider(email_address: str) -> Optional[Dict[str, Any]]:
    """Match the built-in provider default server settings by mail domain. Returns a dict on hit, otherwise None (custom is filled by the user).

    Enterprise mail domains (e.g. ``user@company.com`` hosted on Tencent/NetEase
    enterprise mail) cannot be recognized from the domain alone — we match the
    built-in table exactly; on no match return None, letting the frontend guide
    the user to fill in manually or the backend set provider=custom.
    """
    addr = (email_address or "").strip().lower()
    if "@" not in addr:
        return None
    domain = addr.rsplit("@", 1)[-1]
    hit = _PROVIDERS.get(domain)
    if not hit:
        return None
    provider, ih, ip, isec, sh, sp, ssec = hit
    return {
        "provider": provider,
        "imap_host": ih, "imap_port": ip, "imap_security": isec,
        "smtp_host": sh, "smtp_port": sp, "smtp_security": ssec,
    }


def _map_security(sec: Optional[str]) -> str:
    """External security value → himalaya encryption.type value."""
    s = (sec or "tls").strip().lower()
    if s in ("starttls", "start-tls", "start_tls"):
        return "start-tls"
    if s in ("none", "off", "plain", "insecure"):
        return "none"
    return "tls"


def render_config_toml(
    *, email_address: str, display_name: Optional[str], app_password: str,
    imap_host: str, imap_port: int, imap_security: str,
    smtp_host: str, smtp_port: int, smtp_security: str,
) -> str:
    """Render the single-account himalaya ``config.toml`` (v1.2.0 schema). The app
    password is written in plaintext as ``backend.auth.raw`` — the file itself is
    0600 and lives only in the per-user volume, the same level as lark-cli's
    file-based credentials."""
    def esc(v: str) -> str:
        # TOML basic-string escaping (backslash + double quote). The app password may contain special characters.
        return (v or "").replace("\\", "\\\\").replace('"', '\\"')

    name = display_name or email_address
    imap_enc = _map_security(imap_security)
    smtp_enc = _map_security(smtp_security)
    return (
        f"[accounts.{_ACCOUNT}]\n"
        f"default = true\n"
        f'email = "{esc(email_address)}"\n'
        f'display-name = "{esc(name)}"\n'
        f"\n"
        f'backend.type = "imap"\n'
        f'backend.host = "{esc(imap_host)}"\n'
        f"backend.port = {int(imap_port)}\n"
        f'backend.encryption.type = "{imap_enc}"\n'
        f'backend.login = "{esc(email_address)}"\n'
        f'backend.auth.type = "password"\n'
        f'backend.auth.raw = "{esc(app_password)}"\n'
        # Send IMAP ID (RFC 2971) immediately after login to identify the client:
        # all NetEase mail (163/126/188/yeah) and QQ/Tencent enterprise mail
        # (including many Coremail-based enterprise mail systems) require it,
        # otherwise SELECT on the inbox fails with "Unsafe Login. Please contact
        # kefu@xxx for help". himalaya v1.2.0's email-lib has this capability
        # built in; just flip this switch. No side effects for providers that
        # support ID like Gmail/Outlook.
        # ⚠️ The key name is kebab-case `send-after-auth` (consistent with
        # display-name/encryption.type) — the underscore send_after_auth written
        # in the v1.2.0 sample is wrong; himalaya silently ignores unknown keys →
        # no error, but the ID is never sent and NetEase still reports Unsafe
        # Login. Verified with a real 163 account that the kebab form works.
        f"backend.extensions.id.send-after-auth = true\n"
        f"\n"
        f'message.send.backend.type = "smtp"\n'
        f'message.send.backend.host = "{esc(smtp_host)}"\n'
        f"message.send.backend.port = {int(smtp_port)}\n"
        f'message.send.backend.encryption.type = "{smtp_enc}"\n'
        f'message.send.backend.login = "{esc(email_address)}"\n'
        f'message.send.backend.auth.type = "password"\n'
        f'message.send.backend.auth.raw = "{esc(app_password)}"\n'
        # Do not APPEND a copy into the Sent folder after sending: ① providers
        # name the Sent folder differently / some outright reject IMAP APPEND
        # (e.g. Ethereal returns NO), which would fail the whole send; ② with
        # mainstream providers (Gmail/Outlook, etc.) the server automatically
        # files SMTP-sent mail into Sent, so the client need not store another copy.
        f"message.send.save-copy = false\n"
    )


def _write_config(user_id: str, content: str) -> "Optional[str]":
    """Write config.toml into this user's backend HOME (0600). Returns an error message; None means success."""
    from core.sandbox._common import email_himalaya_config

    cfg = email_himalaya_config(user_id)
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(content, encoding="utf-8")
        os.chmod(cfg, 0o600)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[email] write config failed user=%s: %s", user_id, exc)
        return f"写入邮箱配置失败：{exc}"


def _render_config_from_record(record, secret: str) -> str:
    """Render config.toml from the DB connection record's server metadata + the decrypted app password.
    Shared by the probe fallback (_materialize_config) and cube injection
    (get_email_config_bundle), avoiding two copies of the field mapping."""
    return render_config_toml(
        email_address=record.email_address or "", display_name=record.display_name,
        app_password=secret,
        imap_host=record.imap_host or "", imap_port=int(record.imap_port or 993),
        imap_security=record.imap_security or "tls",
        smtp_host=record.smtp_host or "", smtp_port=int(record.smtp_port or 465),
        smtp_security=record.smtp_security or "tls",
    )


def _email_env(user_id: str) -> Dict[str, str]:
    """Environment for the backend to run himalaya subprocesses: an isolated HOME per user (the root of credential isolation)."""
    from core.sandbox._common import email_home_dir

    home = email_home_dir(user_id)
    try:
        home.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[email] mkdir home %s failed: %s", home, exc)
    env = dict(os.environ)
    env["HOME"] = str(home)
    # himalaya uses XDG; pin it explicitly under this user's HOME to avoid reading the backend process's own config.
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    return env


async def _run_himalaya(
    user_id: str, args: List[str], timeout: int = 40,
) -> Tuple[str, str, int]:
    """Run one himalaya subcommand on the backend as this user; returns (stdout, stderr, exit_code).
    Points ``--config`` explicitly at this user's config.toml to guarantee no account cross-talk."""
    from core.sandbox._common import email_himalaya_config

    cfg = str(email_himalaya_config(user_id))
    try:
        proc = await asyncio.create_subprocess_exec(
            _HIMALAYA_BIN, "--config", cfg, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_email_env(user_id),
        )
    except FileNotFoundError:
        return "", "himalaya binary not found on backend", 127
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return "", f"himalaya {' '.join(args[:2])} timeout", 124


async def _verify(user_id: str) -> Tuple[bool, str]:
    """Connectivity check: ``himalaya folder list`` (actually connects and logs in to IMAP).
    Returns (ok, detail); when ok=False, detail is a stderr summary."""
    out, err, rc = await _run_himalaya(user_id, ["folder", "list", "-o", "json"], timeout=45)
    if rc == 0:
        return True, ""
    if rc == 127:
        return False, "后端未安装 himalaya"
    if rc == 124:
        return False, "连接邮件服务器超时（请检查服务器地址/端口/网络）"
    tail = ((err or out or "").strip())[-300:] or "未知错误"
    return False, tail


class EmailService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = EmailConnectionRepository(db)

    # ── Status ───────────────────────────────────────────────────────────
    def status_dict(self, record) -> Dict[str, Any]:
        return {
            "status": record.status,
            "email_address": record.email_address,
            "display_name": record.display_name,
            "provider": record.provider,
            "imap_host": record.imap_host,
            "imap_port": record.imap_port,
            "imap_security": record.imap_security,
            "smtp_host": record.smtp_host,
            "smtp_port": record.smtp_port,
            "smtp_security": record.smtp_security,
            "last_verified_at": record.last_verified_at.isoformat() if record.last_verified_at else None,
            "last_error": record.last_error,
        }

    def get_status(self, user_id: str) -> Dict[str, Any]:
        return self.status_dict(self.repo.ensure(user_id))

    async def probe_status(self, user_id: str) -> Dict[str, Any]:
        """Real connectivity probe and reconciliation (credentials may have been invalidated by a password change). Unconnected records are not probed."""
        record = self.repo.ensure(user_id)
        if record.status != "connected":
            return self.status_dict(record)
        ok, detail = await self._reverify(user_id, record)
        if ok and record.status != "connected":
            self.repo.update(user_id, {"status": "connected", "last_error": None,
                                       "last_verified_at": datetime.utcnow()})
        elif not ok:
            self.repo.update(user_id, {"status": "error",
                                       "last_error": f"{_VERIFY_FAIL}。详情: {detail}" if detail else _VERIFY_FAIL})
        return self.status_dict(self.repo.get(user_id))

    async def _reverify(self, user_id: str, record) -> Tuple[bool, str]:
        """Before probing, ensure the backend HOME has config.toml (after a restart / machine change only the DB row may remain) — rebuild it from secret_enc."""
        from core.sandbox._common import email_himalaya_config

        if not email_himalaya_config(user_id).exists():
            secret = decrypt_secret(record.secret_enc)
            if not secret or not record.imap_host:
                return False, "凭据缺失，请重新绑定"
            self._materialize_config(user_id, record, secret)
        return await _verify(user_id)

    # ── Binding (synchronous save + verify) ──────────────────────────────
    async def connect(
        self, user_id: str, *, email_address: str, secret: str,
        display_name: Optional[str] = None,
        server_overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Save mailbox credentials and verify synchronously. Success → connected, failure → error (no half-baked credentials left behind)."""
        self.repo.ensure(user_id)
        email_address = (email_address or "").strip()
        secret = secret or ""
        if not email_address or "@" not in email_address:
            self.repo.update(user_id, {"status": "error", "last_error": "邮箱地址格式不正确"})
            return self.status_dict(self.repo.get(user_id))
        if not secret:
            self.repo.update(user_id, {"status": "error", "last_error": "请填写邮箱授权码 / 密码"})
            return self.status_dict(self.repo.get(user_id))

        # Resolve servers: built-in detection first, then apply server_overrides (an override only applies when non-empty).
        detected = detect_provider(email_address) or {
            "provider": "custom",
            "imap_host": None, "imap_port": 993, "imap_security": "tls",
            "smtp_host": None, "smtp_port": 465, "smtp_security": "tls",
        }
        ov = {k: v for k, v in (server_overrides or {}).items() if v not in (None, "")}
        cfg = {**detected, **ov}
        if not cfg.get("imap_host") or not cfg.get("smtp_host"):
            self.repo.update(user_id, {
                "status": "error",
                "last_error": "无法自动识别该邮箱的服务器，请在「高级设置」手填 IMAP / SMTP 服务器地址",
            })
            return self.status_dict(self.repo.get(user_id))

        # Normalized account/server fields — shared by render and the failure/success DB writes, avoiding duplication.
        server_data = {
            "email_address": email_address, "display_name": display_name,
            "provider": cfg.get("provider"),
            "imap_host": cfg["imap_host"], "imap_port": int(cfg["imap_port"] or 993),
            "imap_security": cfg["imap_security"] or "tls",
            "smtp_host": cfg["smtp_host"], "smtp_port": int(cfg["smtp_port"] or 465),
            "smtp_security": cfg["smtp_security"] or "tls",
        }

        # Render + write config.toml to disk
        content = render_config_toml(
            email_address=email_address, display_name=display_name, app_password=secret,
            imap_host=server_data["imap_host"], imap_port=server_data["imap_port"],
            imap_security=server_data["imap_security"],
            smtp_host=server_data["smtp_host"], smtp_port=server_data["smtp_port"],
            smtp_security=server_data["smtp_security"],
        )
        werr = _write_config(user_id, content)
        if werr:
            self.repo.update(user_id, {"status": "error", "last_error": werr})
            return self.status_dict(self.repo.get(user_id))

        # Synchronous verification (really connects to IMAP)
        ok, detail = await _verify(user_id)
        if not ok:
            # Verification failed: remove the just-written config so the sandbox doesn't keep failing with bad credentials
            self._purge_config(user_id)
            self.repo.update(user_id, {
                **server_data,
                "status": "error",
                "secret_enc": None, "config_bundle": None,
                "last_error": f"{_VERIFY_FAIL}。详情: {detail}" if detail else _VERIFY_FAIL,
            })
            return self.status_dict(self.repo.get(user_id))

        # Success: encrypt the app password + store the portable config bundle (for cube injection)
        self.repo.update(user_id, {
            **server_data,
            "status": "connected",
            "secret_enc": encrypt_secret(secret),
            "config_bundle": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "last_verified_at": datetime.utcnow(),
            "last_error": None,
        })
        return self.status_dict(self.repo.get(user_id))

    def _materialize_config(self, user_id: str, record, secret: str) -> None:
        """Rebuild config.toml from DB metadata + the decrypted app password (probe/restart fallback)."""
        _write_config(user_id, _render_config_from_record(record, secret))

    def _purge_config(self, user_id: str) -> None:
        """Delete the himalaya config directory under this user's backend HOME."""
        import shutil

        from core.sandbox._common import email_cache_dir, safe_user_id

        if not safe_user_id(user_id):
            return
        try:
            shutil.rmtree(email_cache_dir(user_id), ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[email] purge config failed user=%s: %s", user_id, exc)

    # ── Disconnect ───────────────────────────────────────────────────────
    async def disconnect(self, user_id: str) -> Dict[str, Any]:
        """Clear the backend persistent credential directory + wipe sensitive DB fields + set disconnected."""
        self._purge_config(user_id)
        self.repo.ensure(user_id)
        self.repo.update(user_id, {
            "status": "disconnected",
            "email_address": None, "display_name": None, "provider": None,
            "imap_host": None, "imap_port": None, "imap_security": None,
            "smtp_host": None, "smtp_port": None, "smtp_security": None,
            "secret_enc": None, "config_bundle": None,
            "last_verified_at": None, "last_error": None,
        })
        return self.status_dict(self.repo.get(user_id))


def get_email_config_bundle(user_id: str) -> Optional[str]:
    """For the sandbox provider (cube) to inject per session: returns base64(config.toml) or None.
    Uses its own DB session, not the request-scoped one."""
    from core.db.engine import SessionLocal

    try:
        with SessionLocal() as db:
            rec = EmailConnectionRepository(db).get(user_id)
            if not rec or rec.status != "connected":
                return None
            if rec.config_bundle:
                return rec.config_bundle
            # Fallback: rebuild on the fly from secret_enc (old data / missing bundle)
            secret = decrypt_secret(rec.secret_enc)
            if not secret or not rec.imap_host:
                return None
            content = _render_config_from_record(rec, secret)
            return base64.b64encode(content.encode("utf-8")).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[email] get config bundle failed user=%s: %s", user_id, str(exc)[:120])
        return None
