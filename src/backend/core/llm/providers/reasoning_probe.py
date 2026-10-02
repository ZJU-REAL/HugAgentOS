"""Configuration-time discovery of reasoning levels; never persists model settings.

A declared list is stronger evidence than HTTP acceptance. A successful probe only
proves that a parameter was accepted, not that a gateway used it during inference.
No network/auth/timeout failure is treated as an unsupported level.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from core.llm.providers.protocol_probe import detect_api_protocol
from core.llm.providers.registry import get_spec
from core.llm.reasoning_effort import EFFORT_KEYS


def _result(
    keys: list[str], source: str, notes: list[str], numeric_range=None, complete=True
) -> dict:
    keys = [key for key in EFFORT_KEYS if key in keys]
    default = (
        "medium" if "medium" in keys else ("high" if "high" in keys else next(iter(keys), None))
    )
    return {
        "levels": [{"key": key, "value": key} for key in keys],
        "default": default,
        "source": source,
        "notes": notes,
        "numeric_range": numeric_range,
        "complete": complete,
    }


def _declared_error(message: str) -> tuple[list[str], list[int] | None]:
    if not re.search(r"reasoning[ _-]?effort", message, re.I):
        return [], None
    # Only interpret explicit accepted-value declarations, never arbitrary quoted text.
    match = re.search(r"(?:should|must|supported|allowed|valid values|accepts).+", message, re.I)
    if not match:
        return [], None
    declaration = match.group()
    keys = [key for key in re.findall(r"['\"]([a-z]+)['\"]", declaration) if key in EFFORT_KEYS]
    bounds = re.search(r"(?:within|range)\s*\[\s*(\d+)\s*,\s*(\d+)\s*\]", declaration, re.I)
    numeric_range = [int(bounds[1]), int(bounds[2])] if bounds else None
    return keys, numeric_range


async def discover_reasoning_efforts(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model_name: str,
    api_protocol: str | None = None,
    timeout: int = 12,
) -> dict[str, Any]:
    spec = get_spec(provider)
    notes: list[str] = []
    if spec.engine != "openai":
        return _result(
            [], "unknown", ["This vendor needs manual configuration of thinking settings."]
        )
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    base_url = base_url.strip().rstrip("/")
    if not base_url or not model_name:
        return _result([], "unknown", ["Model name and endpoint are required."])

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            metadata = await client.get(f"{base_url}/models", headers=headers)
            if metadata.status_code in (401, 403):
                return _result(
                    [], "unknown", [f"Metadata: HTTP {metadata.status_code}; check credentials."]
                )
            if metadata.status_code == 200:
                body = metadata.json()
                rows = body.get("data", []) if isinstance(body, dict) else []
                for row in rows:
                    if not isinstance(row, dict) or row.get("id") != model_name:
                        continue
                    for field in ("supported_reasoning_efforts", "reasoning_efforts"):
                        values = row.get(field)
                        if isinstance(values, list):
                            keys = [
                                value
                                for value in values
                                if isinstance(value, str) and value in EFFORT_KEYS
                            ]
                            if keys:
                                return _result(
                                    keys, "model_metadata", [f"Declared by /models: {field}."]
                                )
        except (httpx.HTTPError, ValueError):
            notes.append("Metadata unavailable; trying small parameter probes.")

        if api_protocol not in ("responses", "chat_completions"):
            if not spec.speaks_responses:
                api_protocol = "chat_completions"
            else:
                detected = await detect_api_protocol(
                    base_url=base_url, api_key=api_key, model_name=model_name, timeout=timeout
                )
                api_protocol = detected.protocol
                if not api_protocol:
                    return _result(
                        [], "unknown", notes + ["Protocol unknown; select a protocol and retry."]
                    )

        accepted: list[str] = []
        complete = True
        # Try medium first: rejected generic defaults often reveal the complete vocabulary,
        # eliminating four unnecessary inference requests.
        for key in ("medium", "low", "high", "xhigh", "max"):
            if api_protocol == "responses":
                url = f"{base_url}/responses"
                payload = {
                    "model": model_name,
                    "input": "Hi",
                    "reasoning": {"effort": key},
                    "max_output_tokens": 32,
                    "stream": True,
                    "store": False,
                }
            else:
                url = f"{base_url}/chat/completions"
                payload = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 32,
                    "stream": True,
                    "chat_template_kwargs": {"thinking": True, "reasoning_effort": key},
                }
                if spec.reasoning_effort_top_level:
                    payload["reasoning_effort"] = key
            try:
                response = await client.post(url, headers=headers, json=payload)
                if response.status_code in (401, 403, 429) or response.status_code >= 500:
                    notes.append(f"{key}: HTTP {response.status_code}; remaining probes skipped.")
                    complete = False
                    break
                if response.status_code == 200:
                    # Some SSE gateways report an error after the HTTP 200 headers.
                    if re.search(
                        r'(?:"(?:type|event)"\s*:\s*"(?:error|response.failed)"|event:\s*(?:error|response.failed)|"error"\s*:\s*\{)',
                        response.text,
                    ):
                        complete = False
                        notes.append(f"{key}: stream reported an error; result unknown.")
                    else:
                        accepted.append(key)
                        notes.append(f"{key}: accepted (actual reasoning effect is not verified).")
                elif response.status_code in (400, 422):
                    declared, bounds = _declared_error(response.text)
                    if declared or bounds:
                        return _result(
                            declared,
                            "validation_error",
                            notes + ["Accepted values declared in parameter validation."],
                            bounds,
                        )
                    complete = False
                    notes.append(f"{key}: rejected; no supported-value declaration.")
                else:
                    complete = False
                    notes.append(f"{key}: HTTP {response.status_code}; result unknown.")
            except httpx.HTTPError as exc:
                notes.append(f"{key}: {type(exc).__name__}; result unknown.")
                complete = False
                # Repeated transport failures cannot add trustworthy evidence.
                break
        return _result(
            accepted, "accepted_probe" if accepted else "unknown", notes, complete=complete
        )
