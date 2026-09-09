"""Bounded gateway observation writer and streaming pass-through instrumentation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from core.db.engine import SessionLocal
from core.db.models.observability import utcnow
from core.services.desktop_observability import ingest
from fastapi.routing import APIRoute

logger = logging.getLogger(__name__)
_tasks = set()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-observation")
dropped = 0


def health_snapshot():
    return {"gateway_pending": len(_tasks), "gateway_dropped": dropped}


def submit(user, device, kind, oid, payload):
    global dropped
    if not user or not device:
        return
    if len(_tasks) >= 128:
        dropped += 1
        logger.warning("desktop gateway observation queue full")
        return

    async def write():
        global dropped
        try:

            def persist():
                with SessionLocal() as db:
                    ingest(
                        db,
                        user,
                        device,
                        {
                            "events": [
                                {
                                    "kind": kind,
                                    "object_id": oid,
                                    "revision": time.time_ns() // 1000,
                                    "payload": payload,
                                }
                            ]
                        },
                        observation="gateway",
                    )

            await asyncio.get_running_loop().run_in_executor(_executor, persist)
        except Exception:
            dropped += 1
            logger.warning("desktop gateway observation persistence failed")

    task = asyncio.create_task(write())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain():
    if _tasks:
        await asyncio.wait(list(_tasks), timeout=3)


class UsageTap:
    """Read small complete SSE lines without retaining or delaying the response body."""

    def __init__(self):
        self.buffer = b""
        self.usage = None
        self.discard_line = False

    def feed(self, chunk):
        if isinstance(chunk, str):
            chunk = chunk.encode()
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if self.discard_line:
                self.discard_line = False
                continue
            if len(line) > 65536:
                continue
            self.parse(line[5:].strip() if line.startswith(b"data:") else line.strip())
        if len(self.buffer) > 65536:
            self.buffer = b""
            self.discard_line = True

    def parse(self, line):
        try:
            body = json.loads(line)
            usage = body.get("usage") if isinstance(body, dict) else None
            if isinstance(usage, dict):
                # Provider counters are snapshots, not token deltas.
                self.usage = {**(self.usage or {}), **usage}
        except (ValueError, TypeError, RecursionError):
            pass

    def finish(self):
        if self.buffer and not self.discard_line:
            self.parse(self.buffer)


class ObservedGatewayRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def observed(request):
            path = request.url.path
            if "/gateway/" not in path or path.endswith(("/authorize", "/receipt")):
                return await handler(request)
            started, tap = time.monotonic(), UsageTap()
            kind = "model" if "/gateway/models/" in path else "tool"
            # A model HTTP attempt gets a distinct id. Tool logical results use the effect result id.
            oid = request.headers.get("x-observation-call-id", "")[:128] or uuid.uuid4().hex
            payload = {
                "chat_id": request.headers.get(
                    "x-observation-chat-id", request.headers.get("x-chat-id", "")
                )[:128],
                "run_id": request.headers.get("x-observation-run-id", "")[:128],
                "message_id": request.headers.get("x-observation-message-id", "")[:128],
                "call_id": oid,
                "execution_location": "cloud",
                "capability_origin": "cloud",
                "created_at": utcnow(),
                "usage_known": False,
            }

            def finish(status):
                payload.update(
                    status=status, duration_ms=round((time.monotonic() - started) * 1000)
                )
                if tap.usage is not None:
                    payload.update(usage=tap.usage, usage_known=True)
                submit(
                    getattr(request.state, "desktop_observation_user", None),
                    request.headers.get("x-desktop-device-id", ""),
                    kind,
                    oid,
                    dict(payload),
                )

            try:
                response = await handler(request)
            except BaseException as exc:
                finish("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
                raise
            payload["provider"] = request.path_params.get("provider_id")
            if kind == "tool":
                payload["mcp_server"] = request.path_params.get("server_id")
                if "application/json" in request.headers.get("content-type", ""):
                    try:
                        body = await request.json()
                        payload.update(
                            tool_name=body.get("tool_name"), tool_args=body.get("arguments")
                        )
                    except (ValueError, RecursionError):
                        pass
                else:
                    payload["tool_name"] = path.rsplit("/", 1)[-1]
            payload["model"] = getattr(request.state, "desktop_observation_model", None)
            status = "success" if response.status_code < 400 else "failed"
            if hasattr(response, "body_iterator"):
                upstream = response.body_iterator

                async def stream():
                    final_status = "cancelled"
                    try:
                        async for chunk in upstream:
                            yield chunk
                            if kind == "model":
                                tap.feed(chunk)
                        tap.finish()
                        final_status = status
                    except Exception:
                        final_status = "failed"
                        raise
                    finally:
                        finish(final_status)

                response.body_iterator = stream()
            else:
                body = getattr(response, "body", b"")
                if kind == "tool":
                    try:
                        payload["tool_result"] = json.loads(body[:65536])
                    except (ValueError, RecursionError):
                        payload["tool_result"] = "[truncated]"
                    data = payload.get("tool_result")
                    if isinstance(data, dict) and isinstance(data.get("data"), dict):
                        if data["data"].get("isError") or data["data"].get("state") == "error":
                            status = "failed"
                finish(status)
            return response

        return observed
