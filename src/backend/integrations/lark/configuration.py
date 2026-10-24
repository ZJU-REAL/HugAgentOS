"""Lark configuration responsibilities."""
from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
from typing import Any, Dict, Optional, Tuple
from core.infra.proc import no_window_kwargs
from integrations.dingtalk.parsing import make_qr_data_uri
import logging
logger = logging.getLogger(__name__)
from integrations.lark import process as lark_process
from integrations.lark import state as lark_state

def lark_app_config() -> Tuple[Optional[str], Optional[str]]:
    """Read the app's app_id / app_secret from "System Config → Lark Workspace" (deployment-level, one app shared by everyone)."""
    try:
        from core.config.store.system_config import SystemConfigService

        svc = SystemConfigService.get_instance()
        app_id = (svc.get("lark.app_id") or "").strip()
        app_secret = (svc.get("lark.app_secret") or "").strip()
        return (app_id or None), (app_secret or None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] read lark config failed: %s", exc)
        return None, None


def app_configured() -> Optional[str]:
    """Whether the shared app has been initialized (the admin's one-click config init --new completed). Returns the app_id or None."""
    from integrations.lark.storage import lark_app_home_dir

    cfg = lark_app_home_dir() / lark_state.APP_CFG_SUB
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        apps = data.get("apps") or []
        if apps and isinstance(apps[0], dict):
            return apps[0].get("appId")
    except Exception:  # noqa: BLE001
        pass
    return None


def seed_user_from_shared_app(user_id: str) -> bool:
    """Copy the shared app base (config.json + master.key + appsecret_*.enc) into the user HOME.

    Copies only once, when the user HOME has no config.json yet (persisted
    afterwards via the bind-mount). master.key and appsecret are copied
    together so that user tokens written by this user's auth login are
    encrypted with — and decryptable by — the same key.
    User token files are never touched (those belong to each individual)."""
    import shutil

    from integrations.lark.storage import lark_app_home_dir
    from integrations.lark.storage import lark_home_dir

    app_home = lark_app_home_dir()
    user_home = lark_home_dir(user_id)
    user_cfg = user_home / lark_state.APP_CFG_SUB
    if user_cfg.exists():
        return True  # base already present
    src_cfg = app_home / lark_state.APP_CFG_SUB
    if not src_cfg.exists():
        return False
    try:
        user_cfg.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_cfg, user_cfg)
        src_store = app_home / lark_state.APP_STORE_SUB
        dst_store = user_home / lark_state.APP_STORE_SUB
        dst_store.mkdir(parents=True, exist_ok=True)
        for name in os.listdir(src_store):
            if name == "master.key" or name.startswith("appsecret_"):
                shutil.copy2(src_store / name, dst_store / name)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[lark] seed user app from shared failed user=%s: %s", user_id, exc)
        return False


async def ensure_app_config(user_id: str) -> Optional[str]:
    """Ensure the user HOME has the app base. Returns an error message; None means ready.

    Prefer the shared app from the **admin's one-click initialization**
    (copying the base into the user HOME; the whole group shares one app);
    without a shared app, fall back to the app_id/secret entered in **System
    Config** (``config init --app-secret-stdin``); with neither → return a
    hint message (the front end guides the admin to initialize).
    """
    # 1) Shared app (admin one-click initialization)
    if app_configured():
        if seed_user_from_shared_app(user_id):
            return None
    # 2) app_id/secret hand-entered in System Config (fallback)
    app_id, app_secret = lark_app_config()
    if app_id and app_secret:
        shown, _, _ = await lark_process.run_lark(user_id, ["config", "show"], timeout=15)
        if app_id in (shown or ""):
            return None
        _, err, rc = await lark_process.run_lark(
            user_id,
            ["config", "init", "--app-id", app_id, "--app-secret-stdin", "--brand", "feishu"],
            timeout=30,
            stdin_data=app_secret,
        )
        if rc != 0:
            logger.warning("[lark] config init failed user=%s rc=%s err=%s", user_id, rc, (err or "")[:200])
            return f"飞书应用配置初始化失败：{(err or '').strip()[:160] or '未知错误'}"
        return None
    return lark_state.NO_APP_ERROR


def app_env() -> Dict[str, str]:
    """Environment for running the shared app's config init: HOME = the shared app HOME; Agent/LARK_APP_* interference stripped."""
    from integrations.lark.storage import lark_app_home_dir

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


async def app_init_flow() -> None:
    """Backend long-running task: ``config init --new`` stream-reads the
    ``open.feishu.cn/page/cli`` configuration URL → echoes it back for the
    admin to scan → blocks while the admin completes it in the browser →
    the app base lands in the shared HOME. Manages its own exceptions."""
    pass  # Shared task state is owned by the state module.
    try:
        proc = await asyncio.create_subprocess_exec(
            lark_state.LARK_BIN, "config", "init", "--new", "--lang", "zh",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=app_env(),
            **no_window_kwargs(),
        )
    except FileNotFoundError:
        lark_state.app_init_state.update({"status": "error", "error": "后端未安装 lark-cli"})
        lark_state.APP_INIT_TASK = None
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
            if lark_state.app_init_state.get("verification_url") is None:
                m = re.search(r"https?://open\.feishu\.cn/\S+", buf)
                if m:
                    url = m.group(0).rstrip(".,;")
                    lark_state.app_init_state.update({
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
        lark_state.APP_INIT_TASK = None
    # Completion check: does the shared HOME now have the app configuration
    aid = app_configured()
    if aid:
        lark_state.app_init_state.update({
            "status": "configured", "app_id": aid,
            "verification_url": None, "qr_data_uri": None, "error": None,
        })
    else:
        lark_state.app_init_state.update({
            "status": "error", "verification_url": None, "qr_data_uri": None,
            "error": "应用配置未完成或已取消，请重试。",
        })


def app_status_dict() -> Dict[str, Any]:
    aid = app_configured()
    if aid:
        return {"configured": True, "app_id": aid, "status": "configured",
                "verification_url": None, "qr_data_uri": None, "error": None}
    return {"configured": False, "app_id": None,
            "status": lark_state.app_init_state.get("status", "idle"),
            "verification_url": lark_state.app_init_state.get("verification_url"),
            "qr_data_uri": lark_state.app_init_state.get("qr_data_uri"),
            "error": lark_state.app_init_state.get("error")}
