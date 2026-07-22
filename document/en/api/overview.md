# API Overview

> Last updated: 2026-07-19

The HugAgentOS backend is a FastAPI application; all business endpoints live under the `/v1/*` prefix. In a production deployment, Nginx strips the `/api/` prefix before forwarding to the backend (see `src/frontend/default.conf.template`), so **the full browser-facing path is `/api/v1/...`**, while hitting the backend container directly uses `/v1/...`. Examples in this document use the local development address `http://localhost:3000/api`.

Related docs: [Authentication](../modules/auth.md) · [Error Codes](error-codes.md) · [Environment Variables](../deployment/environment-variables.md) · [License & Enterprise Edition](../editions/license.md)

## Unified Response Envelope

All `/v1/*` endpoints (except SSE streaming endpoints) return a unified envelope, produced by `src/backend/core/infra/responses.py`:

```json
{
  "code": 10000,
  "message": "Success",
  "data": { "chat_id": "abc123", "title": "New chat" },
  "trace_id": "req_1a2b3c4d5e6f7a8b",
  "timestamp": 1781136000000
}
```

| Field | Type | Description |
|---|---|---|
| `code` | int | 5-digit business code: `10000` success, `10001` created; for the error code scheme see [Error Codes](error-codes.md) |
| `message` | string | Human-readable result description |
| `data` | any | Business payload; on errors, an object with additional context |
| `trace_id` | string | Request trace ID (`req_` + 16 hex chars) for log correlation |
| `timestamp` | int | UTC timestamp in milliseconds |

Paginated endpoints nest `items` + `pagination` inside `data`, produced by `paginated_response()`:

```json
{
  "code": 10000,
  "message": "Success",
  "data": {
    "items": [ { "chat_id": "chat_abc123", "title": "New chat" } ],
    "pagination": {
      "page": 1,
      "page_size": 20,
      "total_items": 42,
      "total_pages": 3,
      "has_previous": false,
      "has_next": true
    }
  },
  "trace_id": "req_...",
  "timestamp": 1781136000000
}
```

> Note: `HTTPException`s raised directly from FastAPI dependencies (e.g. a failed ADMIN_TOKEN check) return FastAPI's native `{"detail": ...}` shape and bypass the envelope; the frontend handles both shapes (`api.ts` reads both `payload.data.login_url` and `payload.detail.data.login_url`).

## Authentication

The backend runs several parallel authentication mechanisms, each covering a different slice of the API surface (implemented in `src/backend/api/deps.py` and `src/backend/core/auth/backend.py`):

| Mechanism | How to send the credential | Scope | Implementation |
|---|---|---|---|
| **Session cookie** | `Cookie: jx_session=<token>` (set automatically on login) | All end-user-facing `/v1/*` endpoints (chats, projects, kb, memories, …) | `get_current_user`: cookie → Redis session lookup |
| **Personal API key** | `Authorization: Bearer sk-jx-...` | Same user identity as a session cookie; intended for scripts / third-party integrations | Recognized by the `sk-jx-` prefix; plaintext returned only once at creation, DB stores SHA256 only (`core/services/api_key_service.py`); self-managed at `/v1/me/api-keys` |
| **Bearer token (mock/remote mode)** | `Authorization: Bearer <token>` | `AUTH_MODE=mock` (development: any token, or none, resolves to the mock user) or `remote` (verified against the user center) | Fallback branch of `get_current_user`; not accepted under `AUTH_MODE=session` |
| **ADMIN_TOKEN** | `Authorization: Bearer <ADMIN_TOKEN>` | `/admin` console backend (`/v1/admin/skills`, `/v1/admin/kb`, …) and content-block write endpoints | `require_admin` (env var `ADMIN_TOKEN`) |
| **CONFIG_TOKEN** | `Authorization: Bearer <CONFIG_TOKEN>` | `/config` console backend (`/v1/config/*`, `/v1/models`, `/v1/service-configs`, some `/v1/admin/*` observability endpoints) | `require_config` (env var `CONFIG_TOKEN`) |
| **ADMIN or CONFIG (either)** | Same as above | Prompt snapshot export/import (`/v1/content/prompts/export|import`) | `require_admin_or_config` |
| **super_admin session** | Session cookie (user with `extra_data.role == "super_admin"`); a valid ADMIN_TOKEN works as fallback | A few cross-user management operations | `require_super_admin` |
| **BACKEND_INTERNAL_TOKEN** | `Authorization: Bearer <token>` | Only `/v1/internal/batch/*` (service-to-service calls); if the env var is unset the endpoint refuses with 503 (fail-closed) | `api/routes/v1/internal_batch.py` |

Resolution order in `get_current_user`: **cookie session → API-key Bearer (`sk-jx-` prefix) → mock/remote Bearer**. A Bearer that looks like an API key but fails validation gets an immediate 401 — it is never silently downgraded to anonymous.

Failed token checks are written to the audit log (`AuditLogRepository.log_denial`); probe traffic carrying no header at all is not persisted, to avoid inflating the `audit_logs` table.

```bash
# As a user (API key)
curl http://localhost:3000/api/v1/chats \
  -H "Authorization: Bearer sk-jx-xxxxxxxx"

# As the admin console (ADMIN_TOKEN)
curl http://localhost:3000/api/v1/admin/skills \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

## SSE Streaming Protocol

The streaming chat endpoint `POST /v1/chats/stream` (`src/backend/api/routes/v1/chats.py`) starts a background run after validation, then follows that run over SSE; after a disconnect, the client can resume from any offset via `GET /v1/chats/stream/{run_id}`. Events are produced by `src/backend/orchestration/workflow.py` and serialized to the wire by `src/backend/orchestration/chat_run_executor.py`.

**Wire format**: `Content-Type: text/event-stream`; each event is one `data: {JSON}` line with the event type in the JSON `type` field (the SSE `event:` line is not used). The stream terminates with `data: [DONE]`. After 15 seconds of silence, an SSE comment line `: heartbeat` keeps reverse-proxy connections alive.

### Request

```bash
curl -N http://localhost:3000/api/v1/chats/stream \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-jx-xxxxxxxx" \
  -d '{
    "chat_id": "chat_abc123",
    "message": "What is the weather in Beijing today?",
    "chat_mode": "fast"
  }'
```

The request body is a `ChatRequest` (`src/backend/api/schemas.py`): `chat_id` and `message` are required; optional fields include `model_name`, `chat_mode` (`fast`/`medium`/`high`/`max`), `attachments`, and `enabled_kbs` / `enabled_skills` / `enabled_mcps` / `enabled_agents`.

### Event types

| `type` | When | Main payload fields |
|---|---|---|
| `run_started` | First frame of the stream | `run_id` (for resume/cancel), `message_id`, `chat_id` |
| `thinking` | Reasoning phase | `message` (phase hint) or `delta` (incremental thinking text) |
| `content` | Answer text delta | `event: "ai_message"`, `delta`, `chat_id` |
| `tool_call` | Agent invokes a tool | `tool_name`, `tool_display_name`, `tool_args`, `tool_id`, `subagent_name?` |
| `tool_result` | Tool returns | `tool_name`, `result` (JSON), `tool_id`, `citations` (citation items) |
| `tool_pending` | Model is buffering tool args / between call start and args | `reason` (e.g. `tool_call_start` / `llm_buffering`) |
| `file_confirm` | A tool is suspended awaiting user confirmation of a "My Space" write | `confirm_id`, `op`, `logical_path`, `message`, `expired`; the stream stays open — the user confirms out-of-band via `POST /v1/chats/{chat_id}/file-confirm` and the tool resumes |
| `batch_confirm` | A batch-execution plan awaits user confirmation | `plan_id`, `total`, `preview`, `default_template`, `placeholder_keys`; confirm via `POST /v1/batch/{plan_id}/confirm` |
| `meta` | Final wrap-up frame of an answer | `route`, `sources`, `artifacts`, `citations`, `warnings`, `is_markdown`, `message_id`, `workspace_files` |
| `error` | Streaming failure | `error` (user-readable message), `chat_id` |

### Sample events

```text
data: {"type": "run_started", "run_id": "run_9f8e7d", "message_id": "msg_001", "chat_id": "chat_abc123"}

data: {"type": "thinking", "message": "Analyzing your question...", "chat_id": "chat_abc123"}

data: {"type": "tool_call", "tool_name": "internet_search", "tool_display_name": "Web Search", "tool_args": {"query": "Beijing weather today"}, "tool_id": "call_01", "chat_id": "chat_abc123"}

data: {"type": "tool_result", "tool_name": "internet_search", "result": {"result": {"query": "Beijing weather today"}}, "tool_id": "call_01", "citations": [{"id": "internet_search-1", "title": "..."}], "chat_id": "chat_abc123"}

data: {"type": "content", "event": "ai_message", "delta": "Beijing is cloudy today, ", "chat_id": "chat_abc123"}

data: {"type": "meta", "route": "main", "sources": [], "artifacts": [], "citations": [...], "warnings": [], "is_markdown": true, "chat_id": "chat_abc123", "message_id": "msg_001", "workspace_files": []}

data: [DONE]
```

`[ref:tool_name-N]` markers in the answer text are parsed into `citations` items by `orchestration/citations.py`; the frontend renders them as citation badges (see [Chat module](../modules/chat.md)).

### Resume and cancel

The answer is generated by a background run; the SSE connection merely *follows* it — disconnecting does not stop generation:

```bash
# Resume after a disconnect (from_offset selects the starting event offset)
curl -N "http://localhost:3000/api/v1/chats/stream/run_9f8e7d?from_offset=0" \
  -H "Authorization: Bearer sk-jx-xxxxxxxx"

# Cancel generation
curl -X POST http://localhost:3000/api/v1/chat-runs/run_9f8e7d/cancel \
  -H "Authorization: Bearer sk-jx-xxxxxxxx"
```

Resume validates run ownership: a non-owner gets 403, a missing run gets 404. `GET /v1/chats/{chat_id}/active-run` reports whether a chat currently has a run in progress (the frontend uses this to reconnect after a page refresh). A run silent for longer than `CHAT_RUN_INACTIVITY_TIMEOUT_SEC` (default 600 s) is considered dead and terminated.

### Other SSE endpoints

The remaining SSE endpoints reuse the same wire format: `GET /v1/batch/{plan_id}/stream` (batch execution progress); plan-mode streams additionally emit `plan_generated` / `plan_error` events. The non-streaming counterpart is `POST /v1/chats/send`, which returns a complete envelope in one response.

## Health Checks

`src/backend/api/health.py`, mounted at the root path (i.e. `/api/health` through Nginx), no auth required:

| Endpoint | Method | Purpose | Behavior |
|---|---|---|---|
| `/health` | GET | Load-balancer health check | Confirms the process is alive only, no dependency checks; returns `{status, service, timestamp}` |
| `/ready` | GET | K8s readiness probe | Checks database / storage / redis / user_center individually; returns 503 `{ready: false, checks: {...}}` if any fails |
| `/live` | GET | K8s liveness probe | Always returns `{alive: true}` |

## Route Catalog

The single source of truth for route registration is `src/backend/api/routes/v1/__init__.py`: the `CE_ROUTERS` tuple lists Community Edition routes, and `EE_ROUTERS` lists Enterprise Edition routes together with their license feature flags. EE routes are physically removed from the CE source tree (the registry silently skips them); in an EE deployment, license feature flags act as a second guard — **requests to an unlicensed feature uniformly return HTTP 402 / code 40201** (see [License](../editions/license.md) for the mechanism).

Auth column legend: "User" = session cookie or personal API key (`get_current_user`); "ADMIN" / "CONFIG" mean the corresponding Bearer token.

### Community Edition (CE) routes

| Group | Module (`api/routes/v1/`) | Prefix | Representative endpoints | Auth |
|---|---|---|---|---|
| Chat & messages | `chats.py` | `/v1/chats` | `POST /stream` (SSE), `GET /stream/{run_id}` (resume), `POST /send` (non-streaming), `GET /`, `GET /{chat_id}/messages`, `POST /{chat_id}/share` | User |
| Chat & messages | `chat_runs.py` | `/v1/chat-runs` | `POST /{run_id}/cancel` | User |
| Chat & messages | `chat_shares.py` | `/v1/chat-shares` | `POST /`, `GET /{share_id}`, `POST /{share_id}/revoke` | User |
| Chat & messages | `summary.py` | `/v1/summary` | `POST /` (chat title summarization) | User |
| Chat & messages | `classify.py` | `/v1/classify` | `POST /` (business-topic classification) | User |
| User & preferences | `users.py` | `/v1` | `GET/PATCH /me`, `POST /me/avatar`, `GET/PUT /users/{id}/preferences` | User |
| User & preferences | `me.py` | `/v1/me` | `GET /teams`, `GET /teams/{id}/members`, `GET /users/search` | User |
| User & preferences | `me_capabilities.py` | `/v1/me` | `POST /mcp-servers`, `POST /skills/upload` (personal custom capabilities) | User |
| User & preferences | `api_keys.py` | `/v1/me/api-keys` | `GET / POST /`, `PATCH/DELETE /{key_id}` | User |
| Personal space | `projects.py` | `/v1/projects` | `GET/POST /`, `POST /{id}/files/upload`, `GET /{id}/chats` | User |
| Personal space | `myspace_folders.py` | `/v1/myspace/folders` | `GET/POST /`, `POST /move-artifact` | User |
| Personal space | `artifacts.py` | `/v1/artifacts` | `GET /`, `GET /favorites`, `DELETE /{artifact_id}` | User |
| Memory | `memories.py` | `/v1/memories` | `GET /`, `DELETE /{memory_id}`, `GET/PATCH /settings`, `GET /profile`, `GET /graph` | User |
| Domain ontology | `ontologies.py` | `/v1/ontologies`, `/v1/admin/ontologies` | User setting/runtime preview; Domain Pack versions, build preflight, gate/review evidence, metrics, and evolution-draft governance | User / ADMIN |
| Capability catalog | `catalog.py` | `/v1/catalog` | `GET /` (capability catalog), `PATCH /{kind}/{id}` | User |
| Capability catalog | `kb.py` | `/v1/catalog/kb` | `POST /`, `POST /{kb_id}/documents`, `GET /{kb_id}/chunks` (personal knowledge bases) | User |
| Capability catalog | `marketplace.py` | `/v1/marketplace` | `GET /skills`, `POST /install`, `POST /submissions` (skill marketplace) | User |
| Capability catalog | `agents.py` | `/v1/agents` | `GET/POST /`, `PUT/DELETE /{agent_id}` (personal agents) | User |
| Models | `models.py` | `/v1/models` | `GET /capabilities` (public); `GET/POST /providers`, `PUT /roles/{role_key}` and other management endpoints | Public / CONFIG |
| Files | `file_upload.py` | `/v1/file` | `POST /upload`, `PUT /{file_id}` | User |
| Files | `file_parse.py` | `/v1/file` | `POST /parse` (document parsing) | User |
| Content blocks | `content.py` | `/v1/content` | `GET /docs` (public read); `PUT /docs/{block_id}` (ADMIN), `GET /prompts/export` / `POST /prompts/import` (ADMIN or CONFIG) | Public / ADMIN / CONFIG |
| Automation | `automations.py` | `/v1/automations` | `GET/POST /`, `POST /{task_id}/trigger`, `GET /notifications/list` | User |
| Batch execution | `batch.py` | `/v1/batch` | `GET /active`, `POST /{plan_id}/confirm`, `GET /{plan_id}/stream` (SSE) | User |
| Batch execution | `internal_batch.py` | `/v1/internal/batch` | `POST /resolve` (service-to-service internal endpoint) | BACKEND_INTERNAL_TOKEN |
| Plan mode | `plans.py` | `/v1/plans` | `POST /generate`, `POST /{plan_id}/execute`, `POST /{plan_id}/cancel` | User |
| System info | `config.py` | `/v1/config` | `GET /tool-names` (tool display-name map, public) | Public |
| System info | `meta.py` | `/v1/meta` | `GET /edition` (edition/mode/feature-flag booleans; public probe, never exposes license details) | Public |
| — (schemas only) | `kb_models.py` | — | No endpoints: shared Pydantic models for the KB routes | — |

### Enterprise Edition (EE) routes

The last column is the license feature flag declared in `EE_ROUTERS`; `—` means explicitly exempt from the guard (these must remain reachable when the license is invalid, otherwise users get stuck in a "402 → logout → login → 402" loop).

| Group | Module | Prefix | Representative endpoints | Auth | Feature |
|---|---|---|---|---|---|
| Audit | `audit.py` | `/v1/audit` | `GET /logs`, `GET /logs/export/csv`, `GET /stats` | User | `audit` |
| Content admin | `admin_skills.py` | `/v1/admin/skills` | `GET/POST /`, `POST /upload`, `PUT /{skill_id}/toggle` | ADMIN | `content_admin` |
| Content admin | `admin_kb.py` | `/v1/admin/kb` | `GET/POST /`, `POST /{kb_id}/documents`, `GET /{kb_id}/chunks` | ADMIN | `content_admin` |
| Content admin | `admin_prompts.py` | `/v1/admin/prompts` | `GET /parts`, `GET/POST /versions`, `POST /preview` | CONFIG | `content_admin` |
| Content admin | `admin_mcp_servers.py` | `/v1/admin/mcp-servers` | `GET/POST /`, `POST /{server_id}/test`, `POST /reload-pool` | CONFIG | `content_admin` |
| Content admin | `admin_agents.py` | `/v1/admin/agents` | `GET/POST /`, `PUT /{agent_id}/toggle`, `GET /export` | ADMIN | `content_admin` |
| Content admin | `admin_skill_drafts.py` | `/v1/admin/skill-drafts` | `GET /`, `POST /{draft_id}/approve` (skill distillation review) | ADMIN | `content_admin` |
| Content admin | `admin_sandbox.py` | `/v1/admin/sandbox` | `GET /deps`, `POST /rebuild` (sandbox dependency rebuild) | ADMIN | `content_admin` |
| Content admin | `admin_marketplace.py` | `/v1/admin/marketplace` | `GET /submissions`, `POST /submissions/{id}/approve` (listing review) | ADMIN | `content_admin` |
| Billing | `admin_usage_logs.py` | `/v1/admin/usage-logs` | `GET /`, `GET /summary`, `GET /models` | CONFIG | `billing` |
| Billing | `admin_billing.py` | `/v1/admin/billing` | `GET /summary`, `GET/POST /pricing` | CONFIG | `billing` |
| Observability & audit | `admin_chat_history.py` | `/v1/admin/chat-history` | `GET /sessions`, `GET /export` (org-wide chat review) | CONFIG | `audit` |
| Observability & audit | `admin_logs.py` | `/v1/admin/logs` | `GET /tools`, `GET /subagents`, `GET /trace/{trace_id}` | CONFIG | `audit` |
| Login & session | `auth.py` | `/v1/auth` | `POST /ticket/exchange` (SSO ticket → session), `GET /session/check`, `POST /logout` | Public (session infrastructure) | — |
| Config console | `config_verify.py` | `/v1/config` | `GET /verify` (CONFIG_TOKEN validation) | CONFIG | — |
| Config console | `edition_ee/routes/config_license.py` | `/v1/config/license` | `GET /` (license details), `POST /` (replace license) | CONFIG | — |
| Multi-tenancy | `edition_ee/routes/config_users.py` | `/v1/config/users` | `GET /`, `PATCH /{user_id}/status`, `POST /{user_id}/reset-password` | CONFIG | `multi_tenancy` |
| Multi-tenancy | `edition_ee/routes/config_teams.py` | `/v1/config/teams` | `GET/POST /`, `POST /{team_id}/members` | CONFIG | `multi_tenancy` |
| Multi-tenancy | `edition_ee/routes/config_invites.py` | `/v1/config/invite-codes` | `GET/POST /`, `POST /{code}/revoke` | CONFIG | `multi_tenancy` |
| Multi-tenancy | `edition_ee/routes/team_files.py` | `/v1/my-teams`, `/v1/teams`, `/v1/artifacts` | `GET /my-teams`, `POST /teams/{id}/files/upload`, `POST /artifacts/{id}/move-to-team` | User + team file permission | `multi_tenancy` |
| System config | `config_security.py` | `/v1/config/security` | `GET /sandbox/overview`, `GET /audit-logs`, `GET /system-health` | CONFIG | `system_config` |
| System config | `service_configs.py` | `/v1/service-configs` | `GET/PUT /`, `POST /test/{group_key}` (external service connectivity tests) | CONFIG | `system_config` |

### Routes outside `/v1`

| Module | Prefix | Representative endpoints | Auth | Notes |
|---|---|---|---|---|
| `api/health.py` | `/` | `GET /health`, `/ready`, `/live` | Public | Health checks |
| `api/routes/files.py` | `/files` | `GET /{file_id}` (download), `GET /{file_id}/preview` | Per file ownership | Generated-file delivery |
| `api/routes/v1/mock_sso.py` (`login_router`) | `/` | `GET/POST /login`, `POST /register` | Public | Local account login/registration (always registered) |
| `api/routes/v1/mock_sso.py` (`mock_sso_router`) | `/mock-sso` | `GET/POST /login`, `POST /ticket/exchange` | Public | Mock SSO pages; registered only in `mock`/`local` login modes |

## Versioning & Compatibility

- There is currently a single API version, `v1`; no separate API version negotiation header exists.
- `GET /v1/meta/edition` is the canonical probe for the deployment flavor (CE/EE, license mode, feature flags); the frontend calls it on startup.
- All timestamps are UTC milliseconds; all response JSON is UTF-8 with non-ASCII characters unescaped (`ensure_ascii=False`).
