"""Run Personal Agent session operations against this backend as the linked account.

Operations go through the ordinary HTTP API over loopback, so routing, permission
checks and run admission are exactly those of the desktop UI.
"""

import json
from typing import Any, Dict

import httpx

from core.config.settings import settings
from hugagent_cli.contract import endpoint

RESPONSE_LIMIT = 2 * 1024 * 1024


async def execute(operation: str, args: Dict[str, Any], headers: Dict[str, str]):
    method, path, body = endpoint(operation, args)
    url = f"http://127.0.0.1:{settings.port}{path}"
    async with httpx.AsyncClient(timeout=35, trust_env=False) as client:
        if operation == "chat.send":
            return await _admitted_run(client, url, body, headers)
        response = await client.request(method, url, json=body, headers=headers)
        response.raise_for_status()
        if len(response.content) > RESPONSE_LIMIT:
            raise RuntimeError("Response exceeds limit")
        if not response.content:
            return {"deleted": True}
        result = response.json()
        return result["data"] if isinstance(result, dict) and "code" in result and "data" in result else result


async def _admitted_run(client: httpx.AsyncClient, url: str, body, headers):
    """Return once the run is admitted. The run continues in the background."""
    async with client.stream("POST", url, json=body, headers=headers) as response:
        response.raise_for_status()
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
                raise RuntimeError("Run admission failed")
    raise RuntimeError("No run acknowledgement received; check active runs before retry")
