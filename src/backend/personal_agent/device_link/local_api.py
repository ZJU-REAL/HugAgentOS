"""Run Personal Agent session operations against this backend as the linked account.

Operations go through the ordinary HTTP API over loopback, so routing, permission
checks and run admission are exactly those of the desktop UI.

Failures are raised as ``OperationFailed`` with the server's own reason; the mailbox turns
them into receipts (hugagent_cli.contract.failure). ``attempt["sent"]`` is set right before
the request that may change something leaves this process, so a failure before it is
known to have changed nothing.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from core.config.settings import settings
from hugagent_cli import contract
from hugagent_cli.contract import endpoint

RESPONSE_LIMIT = 2 * 1024 * 1024
# A 200 event stream means the run was admitted: its user message is committed. If the
# run_started frame takes longer than this, the receipt says admitted instead of waiting on.
ADMISSION_WAIT = 10
REQUEST_TIMEOUT = 35
# The release manifest the desktop shell installs next to this source tree.
BUNDLE_MANIFEST = Path(__file__).resolve().parents[4] / "desktop-bundle.json"


class OperationFailed(Exception):
    """An operation that ended without its data, with the reason the receipt should carry.

    ``receipt``, when given, is the exact receipt; otherwise the mailbox builds one with
    ``contract.failure`` from ``error``/``http_status``/``detail`` and what was sent.
    """

    def __init__(self, error: str, *, http_status: Optional[int] = None, detail: Optional[str] = None,
                 receipt: Optional[dict] = None):
        super().__init__(error)
        self.error, self.http_status, self.detail, self.receipt = error, http_status, detail, receipt


def app_version() -> Optional[str]:
    """The desktop release this server was installed from, or None (e.g. a source checkout)."""
    try:
        version = json.loads(BUNDLE_MANIFEST.read_text(encoding="utf-8")).get("desktop_version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) and version.strip() else None


def server_message(content: bytes) -> str:
    """What the server said about a refused request: JSON detail/message/code, else its text."""
    text = content.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text
    return _readable(data) or text


def _readable(value, depth=0):
    """The human part of an error body: {"detail": {"code", "message"}} → message; 422 lists → their msgs."""
    if isinstance(value, str):
        return value.strip()
    if depth > 3:
        return ""
    if isinstance(value, dict):
        for key in ("message", "detail", "msg", "error", "code"):
            found = _readable(value.get(key), depth + 1) if value.get(key) not in (None, "", [], {}) else ""
            if found:
                return found
        return ""
    if isinstance(value, list):
        return "; ".join(part for part in (_readable(item, depth + 1) for item in value[:5]) if part)
    return str(value) if value is not None else ""


async def _body(response: httpx.Response) -> bytes:
    """The response body, read only up to RESPONSE_LIMIT."""
    chunks, size = [], 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > RESPONSE_LIMIT:
            raise OperationFailed("response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _refused(response: httpx.Response, content: bytes) -> OperationFailed:
    status = response.status_code
    return OperationFailed(f"http_{status}", http_status=status, detail=server_message(content) or None)


def _unwrap(result):
    return result["data"] if isinstance(result, dict) and "code" in result and "data" in result else result


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False)


async def execute(operation: str, args: Dict[str, Any], headers: Dict[str, str], attempt: Optional[dict] = None):
    attempt = {} if attempt is None else attempt
    method, path, body = endpoint(operation, args)
    base = f"http://127.0.0.1:{settings.server.port}"
    async with _client() as client:
        if operation == "chat.send":
            await _chat_exists(client, base, args["chat_id"], headers)
            attempt["sent"] = True
            return await _admitted_run(client, base + path, body, headers, attempt.get("budget"))
        if operation not in contract.READS:
            attempt["sent"] = True
        async with client.stream(method, base + path, json=body, headers=headers) as response:
            try:
                content = await _body(response)
            except OperationFailed:
                status = response.status_code
                if not response.is_success:
                    raise OperationFailed(f"http_{status}", http_status=status, detail="error body too large") from None
                # A read changed nothing; a write that answered this much may well have run.
                receipt = contract.not_executed("response_too_large", detail="use a smaller page_size") \
                    if operation in contract.READS else None
                raise OperationFailed("response_too_large", detail="use a smaller page_size", receipt=receipt) from None
            if not response.is_success:
                raise _refused(response, content)
        if not content.strip():
            return {"deleted": True} if method == "DELETE" else {}
        try:
            return _unwrap(json.loads(content))
        except ValueError:
            raise OperationFailed("invalid_response", http_status=response.status_code,
                                  detail="the local server answered with something other than JSON") from None


async def _chat_exists(client: httpx.AsyncClient, base: str, chat_id: str, headers) -> None:
    """chat.send must never create a chat: the send route makes one for an unknown id."""
    async with client.stream("GET", f"{base}/v1/chats/{chat_id}", headers=headers) as response:
        content = await _body(response)
    if response.status_code == 404:
        raise OperationFailed("chat_not_found", receipt=contract.not_executed(
            "chat_not_found", http_status=404, detail=server_message(content) or f"no chat {chat_id}"))
    if not response.is_success:
        raise _refused(response, content)


async def _admitted_run(client: httpx.AsyncClient, url: str, body, headers, budget: Optional[float] = None):
    """Return once the run is admitted. The run continues in the background."""
    async with client.stream("POST", url, json=body, headers=headers) as response:
        if not response.is_success:
            raise _refused(response, await _body(response))
        # 200: admitted, durably. What follows only names the run.
        admitted = {"admitted": True}
        message_id = response.headers.get("x-hugagent-user-message-id")
        if message_id:
            admitted["user_message_id"] = message_id
        wait = ADMISSION_WAIT if budget is None else max(0.5, min(ADMISSION_WAIT, budget - 1))
        try:
            return await asyncio.wait_for(_run_started(response), wait) or admitted
        except asyncio.TimeoutError:
            return admitted


async def _run_started(response: httpx.Response):
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "run_started" and payload.get("run_id"):
            return payload
        if payload.get("error"):
            error = payload["error"]
            raise OperationFailed("run_failed", detail=error if isinstance(error, str) else json.dumps(error, ensure_ascii=False))
    return None
