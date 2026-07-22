# Error Code Reference

> Last updated: 2026-07-22

This document reflects what is actually implemented in code: business exception classes are defined in `src/backend/core/infra/exceptions.py` (the two EE license-related ones live in `src/backend/edition_ee/licensing/features.py`) and are converted into the [unified response envelope](overview.md#unified-response-envelope) by the global exception handler `src/backend/api/middleware/error_handler.py`. Only **implemented** error codes are listed here; unlisted positions within each range are reserved for future use.

## Error Response Shape

Whenever an `AppException` subclass is raised, `error_handler` produces the standard envelope (the HTTP status comes from the exception definition):

```json
{
  "code": 40001,
  "message": "Resource not found",
  "data": {
    "resource_type": "chat",
    "resource_id": "chat_abc123",
    "hint": "The chat may have been deleted"
  },
  "trace_id": "req_1a2b3c4d5e6f7a8b",
  "timestamp": 1781136000000
}
```

`data` carries per-exception context fields (see the "Extra fields" column below). When troubleshooting, hand the `trace_id` to operations for log lookup first.

> **Exception**: `HTTPException`s raised directly from FastAPI dependencies (e.g. the 401 from `require_admin`, some 401s from `get_current_user`) bypass `error_handler` and return FastAPI's native `{"detail": ...}` shape — `get_current_user` nests the envelope trio (`code`/`message`/`data`) inside `detail`. The frontend handles both shapes.

The actual response when hitting a protected endpoint without logging in (`core/auth/backend.py`, non-envelope shape):

```json
{
  "detail": {
    "code": 30001,
    "message": "Authorization required",
    "data": { "login_url": "/login" }
  }
}
```

A mismatched `require_admin` / `require_config` token is even simpler: just `{"detail": "Unauthorized"}` (HTTP 401); if the corresponding token env var is unset, the response is `{"detail": "... not configured"}` (HTTP 503).

## Code Classification Scheme

5-digit business codes are partitioned by their leading digit (independent of, but roughly aligned with, HTTP status):

| Range | Meaning | Typical HTTP |
|---|---|---|
| `1xxxx` | Success | 200 / 201 |
| `2xxxx` | Client request errors (parameters, files) | 400 |
| `3xxxx` | Authentication & permission errors | 401 / 403 |
| `4xxxx` | Resource state errors (not found, conflict, rate limit, license) | 404 / 409 / 429 / 402 |
| `5xxxx` | Server-side & upstream dependency errors | 500 / 502 / 503 / 504 |

## Success Codes

| code | HTTP | Meaning | Source |
|---|---|---|---|
| 10000 | 200 | Success | Default of `responses.success_response()` |
| 10001 | 201 | Created | `responses.created_response()` |

## Implemented Error Codes

### 2xxxx — Client request errors

| code | HTTP | Exception class | Meaning | Typical trigger | Extra fields (`data`) |
|---|---|---|---|---|---|
| 20001 | 400 | `BadRequestError` / `ValidationError` | Invalid request parameters / validation failure | Missing required fields, malformed field values (both classes share the same code; the latter adds an `errors` list) | `errors?` |
| 21001 | 400 | `FileTooLargeError` | File too large | Upload exceeding the backend size limit (exceeding the Nginx limit instead returns a 413 HTML response with no business code) | `max_size`, `actual_size`, `unit` |
| 21002 | 400 | `InvalidFileTypeError` | Unsupported file type | Uploading an extension/MIME outside the whitelist | `allowed_types`, `actual_type` |

### 3xxxx — Authentication & permissions

| code | HTTP | Exception class | Meaning | Typical trigger | Extra fields |
|---|---|---|---|---|---|
| 30001 | 401 | `AuthenticationError` | Authentication required | No session cookie / Bearer present; the 401 `detail` from `get_current_user` includes a `login_url` for redirect | `login_url?` |
| 30002 | 401 | `InvalidTokenError` | Invalid or expired token / API key | Bearer verification failed; a Bearer with the `sk-jx-` prefix that fails validation | — |
| 30003 | 401 | `TokenExpiredError` | Token expired | Session/ticket expiry | `expired_at`, `hint` |
| 31001 | 403 | `AccessDeniedError` | Access denied | Accessing someone else's resource, insufficient role | `reason?` |
| 31002 | 403 | `InsufficientPermissionsError` | Insufficient permissions | Missing a specific grant (e.g. Lab or API-key enablement) | `required_permission` |
| 31003 | 403 | `ResourceOwnershipError` | Owner-only operation | Deleting/modifying another user's chat, file, or share | `resource_type`, `resource_id`, `reason` |

### 4xxxx — Resource state

| code | HTTP | Exception class | Meaning | Typical trigger | Extra fields |
|---|---|---|---|---|---|
| 40001 | 404 | `ResourceNotFoundError` | Resource not found | chat_id / kb_id / artifact_id does not exist or was deleted | `resource_type`, `resource_id`, `hint` |
| 40002 | 404 | `EndpointNotFoundError` | API endpoint not found | Requesting an unregistered path | `path` |
| 40201 | **402** | `FeatureNotLicensed` | Feature not licensed | A CE / expired-license deployment hitting an EE route (teams, audit, admin consoles, …) — see below | `feature` |
| 40202 | **402** | `SeatLimitExceeded` | Seat limit reached / license invalid for adding users | Registration/invites exceeding the licensed seat count | Varies |
| 41001 | 409 | `ResourceAlreadyExistsError` | Resource already exists | Duplicate-name creation (team, skill ID, …) | `resource_type`, `identifier` |
| 41002 | 409 | `ConcurrentModificationError` | Concurrent modification conflict | Optimistic-lock version mismatch | `expected_version`, `actual_version`, `hint` |
| 42001 | 429 | `RateLimitExceededError` | Rate limit exceeded | Triggered the rate-limiting middleware | `limit`, `retry_after`, `reset_at` |

### 5xxxx — Server-side & upstream

| code | HTTP | Exception class | Meaning | Typical trigger | Extra fields |
|---|---|---|---|---|---|
| 50001 | 500 | `InternalServerError` | Internal server error | Uncategorized runtime failure | `error_type?`, `hint` |
| 50002 | 500 | `DatabaseError` | Database error | DB connection/transaction failure | `error_type`, `hint` |
| 51001 | 500 | `StorageError` | Object storage operation failed | local/S3/OSS read/write failure (`message` reads like `Storage upload failed`) | `error`, `hint` |
| 52001 | 502 | `UserCenterError` | User center error | User-center call failure under `AUTH_MODE=remote` | `error` |
| 52101 | 502 | `ModelAPIError` | Model API error | The LLM endpoint returned an error | `model`, `provider`, `error`, `hint` |
| 52103 | 400 | `ModelAPIRateLimitedError` | Model quota exceeded | Model-side rate limiting / quota exhaustion (note: the implemented HTTP status is 400, not 429) | `model`, `hint` |
| 53001 | 504 | `RequestTimeoutError` | Request timeout | A dependent service timed out | `service`, `timeout` |
| 53003 | 504 | `ModelAPITimeoutError` | Model response timeout | The LLM did not respond in time | `model`, `timeout`, `hint` |
| 54001 | 503 | `ServiceUnavailableError` | Service unavailable | Dependency not ready, feature not configured (e.g. internal endpoint without its token) | — |

## Unlicensed Features (HTTP 402)

EE routes are mounted with license feature guards per the registry in `edition_ee/routes/registry.py` (`edition_ee/licensing/deps.py` → `requires_feature`). When a feature is not licensed, `FeatureNotLicensed` is raised and rendered by `error_handler` as:

```json
{
  "code": 40201,
  "message": "该功能未在当前 license 中授权: multi_tenancy",
  "data": { "feature": "multi_tenancy" },
  "trace_id": "req_...",
  "timestamp": 1781136000000
}
```

Design notes (`edition_ee/licensing/features.py`):

- `FeatureNotLicensed` is the **single source** of the 402 envelope; routes/services must never hand-roll `HTTPException(402)`.
- 402 was chosen over 403 deliberately: the frontend treats 403 as session expiry and forces a logout, and a missing license must not log the user out.
- Three EE routes — `config_verify` / `config_license` / `auth` — are explicitly exempt from the guard: they must remain reachable when the license is invalid, otherwise there is no way to install a new license.

See [License & Enterprise Edition](../editions/license.md).

## Frontend Error Handling (`src/frontend/src/api.ts`)

The unified request function `apiRequest()` handles non-2xx responses in this order:

1. **401 / 403** → invokes the global callback registered via `onUnauthorized()`, taking the login address from `payload.data.login_url` or `payload.detail.data.login_url`, then throws `Error('Session expired')`. **This is exactly why 402 is used instead of 403** — every 403 is treated as session expiry and triggers a logout.
2. **402** → throws `LicenseError`, which the UI renders as a "feature not licensed" notice without logging the user out.
3. **Anything else** → throws a generic `Error`, preferring the envelope's `message` as the text.

Uploads get extra friendly mapping (`uploadErrorMessage()`): HTTP 413 (Nginx `client_max_body_size` exceeded — an HTML response without a business code) maps to a "file too large" notice; business codes `21001` / `21002` generate localized messages from `data.max_size` and `data.allowed_types` respectively.

Errors inside SSE streams do not use HTTP status codes; they are delivered as a `{"type": "error", "error": "..."}` event followed by `data: [DONE]` — see [API Overview · SSE Streaming Protocol](overview.md#sse-streaming-protocol).

## Troubleshooting Tips

1. **Check `code` before the HTTP status**: business codes are finer-grained (e.g. all of `30001` no credential, `30002` invalid credential, and `30003` expired credential map to HTTP 401 but call for different handling).
2. **Search logs by `trace_id`**: every response carries a `trace_id` that the backend's structured logging (`core/infra/logging.py`) propagates end to end; EE deployments can look up the whole chain at `/v1/admin/logs/trace/{trace_id}`.
3. **402 is not an auth problem**: `40201`/`40202` mean the deployed license lacks that feature flag — install a new license via `POST /v1/config/license`; user credentials are irrelevant.
4. **A 413 without a business code is expected**: when the Nginx upload limit is exceeded, the request never reaches the backend, so you get Nginx's HTML error page.
