"""Account-bound model gateway references; bearer credentials never enter model rows."""

from __future__ import annotations

import copy
from urllib.parse import urlsplit
from core.capabilities.errors import CloudUnavailable

PREFIX = "desktop-capability:"
_GATEWAY = "/api/v1/desktop/capability/gateway/models/"


def is_reference(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def gateway_cloud_base(base_url):
    before, sep, after = str(base_url).partition(_GATEWAY)
    parsed = urlsplit(before)
    if (
        not sep
        or not after
        or parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("invalid desktop model gateway URL")
    return before.rstrip("/")


def sanitize_import(data):
    """Accept the legacy shell payload at the boundary, persist only a reference."""
    from core.auth.desktop_bridge import bridge_enabled
    from core.capabilities.paths import capabilities_enabled
    from core.capabilities.ref import profile_id
    from core.services.desktop_capability_protocol import token_subject

    out = copy.deepcopy(data)
    for provider in out.get("providers") or []:
        key = str(provider.get("api_key") or "")
        if not (key.startswith(("dcap1.", "dcap2.")) or is_reference(key)):
            continue
        if not bridge_enabled() or not capabilities_enabled():
            raise ValueError("desktop model references require the local desktop backend")
        base = gateway_cloud_base(provider.get("base_url"))
        if not is_reference(key):
            subject = token_subject(key)
            if not subject:
                raise ValueError("invalid desktop model credential")
            provider["api_key"] = PREFIX + profile_id(base, subject)
    return out


def bind(reference, base_url):
    """Capture identity once per model instance; token rotation stays in memory."""
    from core.services import desktop_cloud_bridge as bridge
    from core.capabilities.ref import profile_id
    from core.services.desktop_capability_protocol import token_subject

    state = bridge.get_state()
    if not state or not is_reference(reference):
        raise CloudUnavailable("sign in again to use the cloud model")
    expected_base = gateway_cloud_base(base_url)
    if expected_base != str(state.get("cloud_base") or "").rstrip("/"):
        raise CloudUnavailable("model belongs to a different cloud instance")
    current = PREFIX + profile_id(expected_base, token_subject(state.get("token") or ""))
    if reference != current:
        raise CloudUnavailable("model belongs to a different cloud account")
    return dict(state)


def headers(reference, base_url, captured_state=None):
    from core.services import desktop_cloud_bridge as bridge

    captured_state = captured_state or bind(reference, base_url)
    bridge.require_current_account(captured_state)
    state = bind(reference, base_url)
    token = str(state.get("token") or "")
    # The signed device claim is decoded only for routing; the cloud validates it.
    import base64, json

    try:
        body = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        device = str(state.get("device_id") or claims.get("device_id") or claims.get("d") or "")
    except (ValueError, IndexError, TypeError):
        device = ""
    return {"Authorization": "Bearer " + token, "X-Desktop-Device-Id": device}


def request_hook(reference, base_url):
    captured = bind(reference, base_url)
    prefix = str(base_url).rstrip("/") + "/"

    async def authorize(request):
        if not str(request.url).startswith(prefix):
            raise CloudUnavailable("cloud model request changed its destination")
        request.headers.update(headers(reference, base_url, captured))

    return authorize, captured


def sync_client_kwargs(reference, base_url, *, timeout=120):
    """Inject the same account guard into synchronous OpenAI/embedding clients."""
    if not is_reference(reference):
        return {}
    import httpx

    captured = bind(reference, base_url)
    prefix = str(base_url).rstrip("/") + "/"

    def authorize(request):
        if not str(request.url).startswith(prefix):
            raise CloudUnavailable("cloud model request changed its destination")
        request.headers.update(headers(reference, base_url, captured))

    return {"http_client": httpx.Client(timeout=timeout, event_hooks={"request": [authorize]})}


def prepare_request_headers(api_key, base_url, captured_state=None):
    if is_reference(api_key):
        return {"Content-Type": "application/json", **headers(api_key, base_url, captured_state)}
    return {"Content-Type": "application/json", "Authorization": "Bearer " + api_key}


def scrub_legacy_rows():
    """One-way upgrade cleanup: remove old capability tokens from device model rows."""
    from core.db.engine import SessionLocal
    from core.db.models import ModelProvider

    with SessionLocal() as db:
        rows = db.query(ModelProvider).filter(ModelProvider.api_key.like("dcap%.%")).all()
        for row in rows:
            try:
                cleaned = sanitize_import(
                    {"providers": [{"api_key": row.api_key, "base_url": row.base_url}]}
                )
                row.api_key = cleaned["providers"][0]["api_key"]
            except ValueError:
                row.api_key = ""
                row.is_active = False
        db.commit()
