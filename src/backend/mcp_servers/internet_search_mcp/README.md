# Internet search MCP

The search tool accepts either `query` or `queries` (1–4 distinct queries), with
`max_results` between 1 and 8 (default 5). A batch uses one configured provider.
Sources are interleaved by query order and conservatively deduplicated by URL.

- `models.py`: strict input contract.
- `providers.py`: provider request/response formats.
- `impl.py`: asynchronous execution, deadlines, retry and bounded backpressure.
- `merge.py`: source ordering, deduplication and content budgets.
- `server.py`: MCP boundary and HTTP client lifespan.

Configure `INTERNET_SEARCH_ENGINE` and its corresponding API key.
Tavily receives `INTERNET_SEARCH_COUNTRY` only when explicitly set and topic is general.
There is no language-filter parameter or implicit country preference.
Invalid/obsolete arguments are rejected, without legacy payload conversion.

The lifespan shares an async HTTP client and an eight-request semaphore.
At most 32 queries may be active or queued; the batch deadline is 45 seconds.
429 and retryable 5xx responses get at most two retries within that deadline.
Retry-After is respected, including HTTP-date values; backoff never holds a request slot.
Cancellation drains all batch tasks. Errors expose codes, never upstream response bodies.

Only `result.results` holds sources for citation injection. Partial failures have per-query
statuses and no top-level error; complete failure is an explicit error.
Content budgets are 2,000 characters per snippet, 8,000 per raw body and 24,000 total,
fairly allocated across selected sources. Known result limits and content truncation are separate.

Run with the repository's installed dependencies:

```bash
PYTHONPATH=src/backend python -m mcp_servers.internet_search_mcp.server
PYTHONPATH=src/backend python -m mcp_servers.internet_search_mcp._selftest
```
