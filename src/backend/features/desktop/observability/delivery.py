"""HTTP delivery boundary for durable desktop observations."""

from __future__ import annotations
from features.desktop.bridge import identity as bridge_identity

import logging
import time

import httpx
from core.infra.responses import generate_trace_id
from features.desktop.bridge import client as bridge

logger = logging.getLogger(__name__)


async def deliver_batch(state, batch, *, transport=None):
    # Headers and the rejection snapshot must name the same exact token, even
    # if the shell rotates credentials while a queued batch is being prepared.
    with bridge_identity.account_scope(state):
        credential = dict(bridge_identity.current_credentials(state))
        headers = bridge_identity.cloud_headers(credential)
    trace_id = generate_trace_id()
    headers["X-Trace-ID"] = trace_id
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(8.0, connect=3.0),
        transport=transport,
        follow_redirects=False,
    ) as client:
        request = client.build_request(
            "POST",
            credential["cloud_base"].rstrip("/") + "/api/v1/desktop/observability/batch",
            headers=headers,
            json=batch,
        )
        started = time.monotonic()
        status = None
        stage = "request"
        try:
            reply = await client.send(request)
            status = reply.status_code
            reply.raise_for_status()
            stage = "decode"
            body = reply.json()
            if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
                raise ValueError("invalid desktop observation response")
            result = body["data"]
            acks = result.get("acknowledged")
            if not isinstance(acks, list) or any(not isinstance(a, dict) for a in acks):
                raise ValueError("invalid desktop observation acknowledgements")
            return body
        except Exception as exc:
            bridge_identity.note_rejected_credential(credential, exc)
            # Neither exception text nor remote fields are safe diagnostic data.
            # The locally generated trace also reaches the cloud request logger.
            logger.warning(
                "desktop log upload failed trace_id=%s stage=%s error_type=%s "
                "http_status=%s elapsed_ms=%d event_count=%d batch_bytes=%d",
                trace_id,
                stage,
                type(exc).__name__,
                status,
                round((time.monotonic() - started) * 1000),
                len(batch["events"]),
                len(request.content),
            )
            raise
