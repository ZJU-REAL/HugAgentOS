"""Audience-bound invocation proofs; third-party MCPs never receive the key."""
import base64
import hashlib
import hmac
import json
import os
import time

HEADER = "X-Hugagent-Invocation"

def key():
    value = os.getenv("BACKEND_INTERNAL_TOKEN", "")
    if not value:
        from core.config.local_mode import local_mode_enabled
        if local_mode_enabled():
            value = os.getenv("SANDBOX_RUNNER_TOKEN", "")
    if not value:
        raise ValueError("mcp_invocation_authentication_unconfigured")
    return value.encode()

def issue(audience, user_id, chat_id):
    body = base64.urlsafe_b64encode(json.dumps(
        {"aud": audience, "user": user_id, "chat": chat_id, "expires": time.time() + 90},
        separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(key(), body.encode(), hashlib.sha256).hexdigest()
    return {HEADER: body + "." + signature}

def for_url(url, user_id, chat_id):
    from core.config.mcp_config import _mcp_http_url
    from mcp_servers._ports import PORTS
    audience = next((name for name in PORTS if _mcp_http_url(name).rstrip("/") == str(url).rstrip("/")), None)
    if not audience:
        return {}
    try:
        return issue(audience, user_id or "", chat_id or "")
    except ValueError:
        return {}

def verify(headers, audience):
    value = headers.get(HEADER, "")
    try:
        body, signature = value.split(".")
        expected = hmac.new(key(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid_signature")
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if claims["aud"] != audience or not time.time() < claims["expires"] <= time.time() + 95:
            raise ValueError("invalid_audience_or_expiry")
        if claims["user"] != headers.get("x-current-user-id") or claims["chat"] != headers.get("x-chat-id"):
            raise ValueError("identity_mismatch")
        return claims
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("mcp_invocation_not_authorized") from exc
