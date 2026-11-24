"""Shared plumbing for the embedding and reranker HTTP endpoints."""
from __future__ import annotations

import os
import threading
from typing import Optional

import requests

_SESSION: Optional[requests.Session] = None
_SESSION_LOCK = threading.Lock()


def http_session() -> requests.Session:
    """One pooled session per process so batched calls reuse TCP/TLS connections."""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = requests.Session()
    return _SESSION


def resolve_service_config(
    role: str, env_url: str, env_model: str, env_key: str, *, failure_site: str
) -> tuple[str, str, str]:
    """Resolve ``(base_url, model, api_key)`` for a model role from DB, with env fallback."""
    try:
        from core.models.config import ModelConfigService

        cfg = ModelConfigService.get_instance().resolve(role)
        if cfg:
            return cfg.base_url.rstrip("/"), cfg.model_name, cfg.api_key
    except Exception as fallback_error:
        from core.infra.failures import record_best_effort_failure

        record_best_effort_failure(failure_site, fallback_error)
    return (
        os.getenv(env_url, "").rstrip("/"),
        os.getenv(env_model, ""),
        os.getenv(env_key, ""),
    )


def endpoint_headers(api_key: str, base_url: str) -> dict:
    from core.harness.registry.model_credentials import prepare_request_headers

    return prepare_request_headers(api_key, base_url)
