"""Bearer identities for AUTH_MODE=mock / remote.

Resolving a Bearer token is two steps with different costs. Verifying it is a
round trip to the user center (remote) or a pure function of the token (mock);
binding the identity to a ``users_shadow`` row is local database work. Both used
to run synchronously on the event loop for every request, so one slow user
center stalled every stream the worker was serving.

Verification is now asynchronous, and a verified token is remembered for
``VERIFIED_TOKEN_TTL_SECONDS`` in the shared TTL keyspace, so every worker
reuses it and the user center sees one request per token per window. That
window is also how long a token revoked at the user center keeps working here.
Rejections are never remembered: a token that failed may be valid a moment later.
The cache is an optimization; when the store is unavailable the token is simply
verified again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import ssl
import weakref
from typing import Any, Optional

import httpx

from core.config.settings import settings
from core.infra.ephemeral import get_ephemeral_state
from core.infra.failures import record_best_effort_failure
from fastapi import HTTPException

VERIFIED_TOKEN_TTL_SECONDS = 60
MOCK_TOKEN = "mock_token"
_CACHE_PREFIX = "auth:bearer:"
# The fields the backend reads. Only these are cached, so whatever else the user
# center returns never lands in the shared store.
_IDENTITY_FIELDS = ("user_center_id", "username", "email", "avatar_url")
# Per event loop, the verification already in flight for each token: a burst of
# requests carrying one uncached token shares a single call to the user center.
_in_flight: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Task]]" = (
    weakref.WeakKeyDictionary()
)


def mock_identity(token: str) -> dict[str, Any]:
    """Mock authentication for development: the token names the user."""
    if token and token != MOCK_TOKEN:
        return {
            "user_center_id": token,
            "username": token,
            "email": f"{token}@mock.local",
            "avatar_url": None,
        }
    return {
        "user_center_id": settings.auth.mock_user_id,
        "username": settings.auth.mock_username,
        "email": "dev@example.com",
        "avatar_url": None,
    }


def _identity(payload: Any) -> Optional[dict[str, Any]]:
    """The identity in a user center reply, or None when the reply names nobody."""
    if not isinstance(payload, dict) or not payload.get("user_center_id") or "username" not in payload:
        return None
    return {name: payload.get(name) for name in _IDENTITY_FIELDS}


def _tls_verification() -> ssl.SSLContext | bool:
    """Trust the CA bundle operators point ``requests`` at, which httpx does not read."""
    bundle = os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE")
    return ssl.create_default_context(cafile=bundle) if bundle else True


async def _ask_user_center(token: str) -> Optional[dict[str, Any]]:
    """The user center's verdict: the identity it vouches for, or None when it rejects the token."""
    url = settings.auth.api_url
    if not url:
        raise HTTPException(
            status_code=500,
            detail={"code": 52002, "message": "AUTH_API_URL not configured", "data": {}},
        )
    attempts = settings.auth.retry_count
    async with httpx.AsyncClient(
        timeout=settings.auth.api_timeout, follow_redirects=True, verify=_tls_verification()
    ) as client:
        for attempt in range(attempts):
            try:
                response = await client.get(
                    f"{url}/verify", headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as error:
                if attempt == attempts - 1:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "code": 52001,
                            "message": "User center unavailable",
                            "data": {"error": str(error)},
                        },
                    ) from None
                continue
            if response.status_code == 200:
                return _identity(response.json())
            if response.status_code == 401:
                return None
    return None


async def verify_bearer(token: str) -> tuple[Optional[dict[str, Any]], bool]:
    """Verify a Bearer token under the configured mode.

    Returns ``(identity, checked)``: ``identity`` is None when the token is
    rejected, and ``checked`` is True only for the one call that asked the user
    center, which is what a login audit records.
    """
    if settings.auth.mode == "mock":
        return mock_identity(token), False
    key = _CACHE_PREFIX + hashlib.sha256(token.encode()).hexdigest()
    state = get_ephemeral_state()
    try:
        cached = await state.get(key)
    except Exception as error:  # noqa: BLE001 - a cache outage must not fail authentication
        record_best_effort_failure("core.auth.bearer:verify_bearer.get", error)
        cached = None
    if cached is not None:
        return json.loads(cached), False
    pending = _in_flight.setdefault(asyncio.get_running_loop(), {})
    task = pending.get(key)
    if task is not None:
        # Shielded: a waiter whose client disconnects must not cancel the others' answer.
        return await asyncio.shield(task), False
    task = asyncio.ensure_future(_verify_and_remember(token, key, state))
    pending[key] = task
    task.add_done_callback(lambda _task: pending.pop(key, None))
    return await asyncio.shield(task), True


async def _verify_and_remember(token: str, key: str, state) -> Optional[dict[str, Any]]:
    identity = await _ask_user_center(token)
    if identity is not None:
        try:
            await state.put(key, json.dumps(identity), ttl=VERIFIED_TOKEN_TTL_SECONDS)
        except Exception as error:  # noqa: BLE001 - the next request verifies again
            record_best_effort_failure("core.auth.bearer:verify_bearer.put", error)
    return identity
