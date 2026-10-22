"""DingTalk application environment for CLI subprocesses."""
import logging
logger = logging.getLogger(__name__)

def dws_extra_envs() -> dict[str, str]:
    """Return deployment-level dws Custom App environment variables.

    This helper lives in the provider-neutral sandbox module because both the
    backend OAuth subprocess and optional sandbox providers need it.  Keeping
    it here also lets DingTalk configuration load without importing any concrete
    sandbox provider.
    """
    out: dict[str, str] = {}
    try:
        from core.config.store.system_config import SystemConfigService

        svc = SystemConfigService.get_instance()
        client_id = (svc.get("dingtalk.client_id") or "").strip()
        client_secret = (svc.get("dingtalk.client_secret") or "").strip()
        trusted_domains = (svc.get("dingtalk.trusted_domains") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dws] read dingtalk config failed: %s", exc)
        return out
    if client_id and client_secret:
        out["DWS_CLIENT_ID"] = client_id
        out["DWS_CLIENT_SECRET"] = client_secret
    if trusted_domains:
        out["DWS_TRUSTED_DOMAINS"] = trusted_domains
    return out
