"""Bounded asynchronous search batches, with one provider selected per batch."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import anyio
import httpx
from core.config.runtime_env import get_runtime_value

from .merge import merge_sources
from .models import SearchRequest
from .providers import KEY_NAMES, ProviderConfig, build_request, normalize_response

logger = logging.getLogger(__name__)
BATCH_TIMEOUT = 45.0
REQUEST_TIMEOUT = 20.0
MAX_RETRIES = 2
MAX_INFLIGHT = 8
MAX_PENDING = 32
MAX_RESPONSE_BYTES = 2_000_000


class ResponseTooLarge(ValueError):
    pass


RETRY_STATUSES = {429, 500, 502, 503, 504}


def retry_delay(response: httpx.Response, attempt: int) -> float:
    delay = 2.0**attempt
    value = response.headers.get("Retry-After")
    if value:
        try:
            delay = max(delay, float(value))
        except ValueError:
            try:
                delay = max(
                    delay,
                    (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds(),
                )
            except (ValueError, TypeError, OverflowError):
                pass
    return delay + random.uniform(0, 0.75)


class SearchService:
    """Owns process-local backpressure; the server lifespan owns the HTTP client."""

    def __init__(self, client: httpx.AsyncClient, *, batch_timeout: float = BATCH_TIMEOUT):
        self.client = client
        self.batch_timeout = batch_timeout
        self._slots = asyncio.Semaphore(MAX_INFLIGHT)
        self._pending = 0

    async def search(self, request: SearchRequest) -> dict:
        queries = request.query_list()
        if self._pending + len(queries) > MAX_PENDING:
            raise RuntimeError("Search capacity is busy; retry later")
        engine = (get_runtime_value("INTERNET_SEARCH_ENGINE") or "tavily").strip().lower()
        if engine not in KEY_NAMES:
            raise ValueError("Unsupported internet search engine")
        key = (get_runtime_value(KEY_NAMES[engine]) or "").strip()
        if not key:
            raise RuntimeError(f"{KEY_NAMES[engine]} is not configured")
        config = ProviderConfig(
            engine,
            key,
            (get_runtime_value("INTERNET_SEARCH_COUNTRY") or "").strip(),
            (get_runtime_value("INTERNET_SEARCH_AUTO_PARAMETERS") or "").strip().lower()
            in {"1", "true", "yes", "on"},
        )
        self._pending += len(queries)
        tasks = []
        try:
            deadline = asyncio.get_running_loop().time() + self.batch_timeout
            tasks = [
                asyncio.create_task(self._query(config, query, request, deadline))
                for query in queries
            ]
            outcomes = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            try:
                with anyio.CancelScope(shield=True):
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._pending -= len(queries)
        statuses = [
            {
                "index": index,
                "status": "error" if "error_code" in outcome else "ok",
                "returned_count": len(outcome["results"]),
                **({"error_code": outcome["error_code"]} if "error_code" in outcome else {}),
            }
            for index, outcome in enumerate(outcomes)
        ]
        failures = sum("error_code" in outcome for outcome in outcomes)
        return {
            "status": (
                "error" if failures == len(queries) else "partial" if failures else "complete"
            ),
            "queries": queries,
            "provider": engine,
            "query_statuses": statuses,
            **merge_sources(outcomes, request.max_results),
        }

    async def _query(
        self, config: ProviderConfig, query: str, options: SearchRequest, deadline: float
    ) -> dict:
        try:
            async with asyncio.timeout_at(deadline):
                url, headers, payload = build_request(config, query, options)
                for attempt in range(MAX_RETRIES + 1):
                    # Waiting and backoff are covered by the deadline. Never sleep holding a slot.
                    async with self._slots:
                        remaining = deadline - asyncio.get_running_loop().time()
                        response, data = await self._post(
                            url,
                            headers,
                            payload,
                            min(REQUEST_TIMEOUT, max(0.001, remaining)),
                        )
                    if response.status_code not in RETRY_STATUSES or attempt == MAX_RETRIES:
                        break
                    delay = retry_delay(response, attempt)
                    if delay >= deadline - asyncio.get_running_loop().time():
                        response.raise_for_status()
                    await asyncio.sleep(delay)
                response.raise_for_status()
                return normalize_response(config.engine, data)
        except (TimeoutError, httpx.TimeoutException):
            code = "timeout"
        except httpx.HTTPStatusError as exc:
            code = f"http_{exc.response.status_code}"
        except httpx.RequestError:
            code = "network_error"
        except ResponseTooLarge:
            code = "response_too_large"
        except (ValueError, TypeError, AttributeError):
            code = "invalid_response"
        # Do not expose upstream bodies, request headers, or credential-bearing exception messages.
        logger.warning("Search query failed provider=%s code=%s", config.engine, code)
        return {"results": [], "error_code": code, "upstream_truncated": None}

    async def _post(self, url: str, headers: dict, payload: dict, timeout: float):
        async with self.client.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
            timeout=timeout,
        ) as response:
            if not response.is_success:
                return response, None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ResponseTooLarge("Search response exceeded byte budget")
                body.extend(chunk)
            return response, json.loads(body)
