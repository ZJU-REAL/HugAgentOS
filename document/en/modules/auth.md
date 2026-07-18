# Authentication & Permissions

> Last updated: 2026-06-11

The authentication and permission system in HugAgentOS covers three independent tracks: **end-user authentication** (local accounts / mock SSO / enterprise SSO / personal API keys), **administrative credentials** (the `ADMIN_TOKEN` and `CONFIG_TOKEN` Bearer tokens), and **resource-level permissions** (fine-grained access control over team files, projects, and chat shares). The implementation lives in `src/backend/core/auth/`, with the FastAPI dependency-injection entry points in `src/backend/api/deps.py`.

The Community Edition (CE) is built around a single-user / single-tenant model; organization-scale capabilities such as teams, invite codes, and enterprise SSO belong to the **Enterprise Edition (EE)** and are double-gated by the route registry (`api/routes/v1/__init__.py`) and license feature flags — see [Editions & Licensing](../editions/overview.md).

## Authentication modes (AUTH_MODE)

The `AUTH_MODE` environment variable determines how the backend verifies request identity, implemented in `core/auth/backend.py`. **Three** modes are actually supported (not the two often quoted in docs):

| Mode | Use case | Verification |
|---|---|---|
| `mock` (default) | Local development | Any Bearer token is treated as a login under that username; without a token, a default dev user is injected (`AUTH_MOCK_USER_ID` / `AUTH_MOCK_USERNAME`) |
| `remote` | External user-center integration (legacy) | Bearer token is verified against `{AUTH_API_URL}/verify` with retries (`AUTH_RETRY_COUNT`) and timeout (`AUTH_API_TIMEOUT`) |
| `session` | Production main path (SSO ticket login) | Validates the `jx_session` cookie against a Redis session; plain Bearer tokens are **not** accepted |

The resolution priority in `get_current_user` (`core/auth/backend.py`) is the same across all modes:

1. **Cookie session** (`jx_session` → Redis lookup) — tried first in every mode; a present-but-expired cookie returns a hard 401 (code `30003`) without fallback.
2. **Personal API key** (Bearer token with the `sk-jx-` prefix) — valid in all modes; anything that looks like an API key but fails validation is rejected with 401, never silently downgraded to anonymous.
3. **Bearer token** — rejected in `session` mode; in `mock` / `remote` mode it goes through mock verification or the remote `verify` call respectively.

401 responses always carry a `login_url` (derived automatically per deployment by `settings.sso.effective_login_url`) that the frontend uses to redirect to the login page.

## Login methods

### Local accounts (Community Edition default)

With `LOCAL_AUTH_ENABLED=true` (default), the `login_router` in `api/routes/v1/mock_sso.py` serves a unified login page:

- `GET /login` — renders a login / registration tabbed page (the registration tab only appears when local accounts are enabled)
- `POST /login` — username/password login
- `POST /register` — local account registration (optionally requiring an invite code, see below)

Password hashing lives in `core/auth/password.py`; the minimum length is controlled by `PASSWORD_MIN_LENGTH` (default 8). Local accounts are stored in the `local_users` table (`LocalUser` in `core/db/models`), linked one-to-one to the shadow user table.

### Mock SSO (development)

With `SSO_MOCK_ENABLED=true`, the `/mock-sso/*` routes (`api/routes/v1/mock_sso.py`) are registered, simulating the full ticket flow of an external unified login system:

```
GET  /mock-sso/login            → generates a one-time ticket and redirects back with ?ticket=...
POST /mock-sso/ticket/exchange  → validates the ticket and returns user info
```

Tickets are stored in the in-process `core/auth/mock_ticket_store.py`, consumed by `core/auth/sso.py` in mock mode. To test manually, open `http://localhost:3001/mock-sso/login?redirect=/` in a browser.

### Enterprise SSO (Enterprise Edition)

Production SSO uses a ticket-exchange flow implemented in `core/auth/sso.py`, **gated by the license feature `Feature.SSO`**:

- `POST /v1/auth/ticket/exchange` — exchange a one-time ticket for a session (`api/routes/v1/auth.py`)
- `GET /v1/auth/sso/authorize-url` — proxies the SSO provider's OAuth authorize URL; guarded at the route level with `requires_feature(Feature.SSO)`
- `GET /v1/auth/session/check` / `POST /v1/auth/logout` — session check and logout (infrastructure, never license-gated)

`SSO_EXCHANGE_MODE` switches between real mode (`GET {SSO_TICKET_EXCHANGE_URL}?{callback_param}={credential}`) and mock mode; `SSO_CALLBACK_PARAM` supports both `ticket` (legacy) and `code` (the OAuth2-style provincial SSO) parameter names.

### Session management

`core/auth/session.py` owns the session lifecycle:

- Redis key format `jx:session:{sha256(token)}`, value is JSON-encoded user data
- TTL controlled by `SESSION_TTL_HOURS` (default 8 hours)
- `SESSION_STORE_TYPE=memory` degrades to an in-process dict (minimal deployments without Redis)

> When the CE-derived tree physically lacks `core/auth/session.py`, the cookie resolution in `backend.py` quietly short-circuits to the Bearer / mock path (seam C5).

## User system

External identities (SSO / user center) are decoupled from local business data through the **shadow user table** `users_shadow` (ORM model `UserShadow`): on first successful authentication, `UserService.get_or_create_user_shadow()` creates the row, and all business tables (chats, files, memories, etc.) reference it via the `user_id` foreign key.

User-facing endpoints:

| Endpoint | File | Description |
|---|---|---|
| `GET/PATCH /v1/me` | `api/routes/v1/users.py` | Profile (incl. department, teams, local account data); local accounts may edit nickname / real name / phone |
| `POST/PUT/DELETE /v1/me/avatar` | `api/routes/v1/users.py` | Avatar upload (≤2 MB) / set / clear |
| `GET/PUT /v1/users/{id}/preferences` | `api/routes/v1/users.py` | User preferences |
| `GET /v1/me/teams` etc. | `api/routes/v1/me.py` | User-side team viewing, member invitation, removal / leaving (Enterprise Edition; degrades to 404 in CE trees lacking the team module) |
| `GET /v1/me/users/search` | `api/routes/v1/me.py` | User search for invitations |

## Permission system

### Interface layer (CE/EE split seam)

`core/auth/permissions_iface.py` is the **single import point** for permission symbols: mainline code (deps / files / chats / projects / kb, etc.) imports only from here, never from the concrete implementations. In the EE main repo it purely re-exports three real implementations; in the CE-derived tree the whole file is replaced by a single-tenant stub and the three implementation files do not physically exist:

| Implementation file | Responsibility |
|---|---|
| `core/auth/team_permissions.py` | Team folder permission resolution (Enterprise Edition) |
| `core/auth/project_permissions.py` | Project access (team projects are Enterprise Edition) |
| `core/auth/chat_share_permissions.py` | Chat access / deletion / share-scope permissions |

`resolve_artifact_access(db, user_id, owner_id, team_id)` is the unified owner ∪ team access-level resolver: owner is always `admin` → team members follow team permission → everyone else gets `none`. File download (`api/routes/files.py`), knowledge base, My Space, and all other artifact access points share it.

### Team roles and file permissions (Enterprise Edition)

`core/auth/roles.py` defines three team roles: `owner` > `admin` > `member`. The team file permission mapping (`team_permissions.py`):

| Team role | File permission | Allowed actions |
|---|---|---|
| owner / admin | `admin` | Everything: upload / delete any file, manage folders, configure member permissions |
| member + editor | `edit` | Upload, delete own uploads, move files in from personal space |
| member + viewer | `view` | Read-only |
| non-member | `none` | No access |

Routes consume them through the dependency factories in `api/deps.py`: `require_team_role(min_role)` and `require_team_file_perm(min_permission)` (yielding a `TeamFileAccess` context). Every denial is written to the audit log via `AuditLogRepository.log_denial()`.

### Per-user permission flags

User-granular feature switches are stored in the `users_shadow.metadata` JSON column (ORM attribute `extra_data`) and set from the user management module of the Config console (`api/routes/v1/config_users.py`). **All default to off** (turning a flag off removes the key from metadata):

| Flag | Default | Control endpoint | Gates |
|---|---|---|---|
| `can_use_api_key` | off | `PATCH /v1/config/users/{id}/api-key-permission` | Creating / using personal API keys; turning it off immediately invalidates existing keys |
| `can_add_skill` | off | `PATCH /v1/config/users/{id}/skill-permission` | Self-service private skill upload / authoring in the capability center (`api/routes/v1/me_capabilities.py`) |
| `can_add_mcp` | off | `PATCH /v1/config/users/{id}/mcp-permission` | Self-service private remote MCP servers (HTTP/SSE) |
| `lab_enabled` | **on** | `PATCH /v1/config/users/{id}/lab-permission` | Lab module entry and access |
| `allowed_apps` | unrestricted | `PATCH /v1/config/users/{id}/app-permissions` | App visibility; `None` = all enabled apps, list = allowlist (empty list = block all) |
| `role: super_admin` | none | (written directly into metadata) | Passes the `require_super_admin` dependency; bypasses team role checks |

Self-added private MCP servers / skills record `owner_user_id` = the current user and are **visible and usable only to that user** (owner isolation).

## Administrative credentials: ADMIN_TOKEN and CONFIG_TOKEN

The platform has two independent administrative Bearer tokens, matching the two frontend consoles `/admin` (operations console) and `/config` (system console). The dependency implementation is the `_require_token` factory in `api/deps.py`, producing `require_admin` and `require_config`; if the corresponding environment variable is unset the endpoint returns 503, and a mismatched token returns 401 with an audit entry (only attempts that *carry* a header with a wrong token are logged — bare probing is not, to keep the audit table from being DoS-amplified).

The actual gating, verified against each route file's dependencies:

| Credential | Gated route groups |
|---|---|
| `ADMIN_TOKEN` (`require_admin`) | `admin_skills` (skill management), `admin_skill_drafts` (distilled draft review), `admin_marketplace` (marketplace review), `admin_kb` (public KB management), `admin_sandbox` (sandbox dependency rebuilds), `admin_agents` (sub-agents), and the content-block writes in `content.py` (e.g. `PUT /v1/content/docs/{block_id}`, manual upload) |
| `CONFIG_TOKEN` (`require_config`) | `admin_prompts` (prompt management), `admin_mcp_servers` (MCP management), `admin_billing` / `admin_usage_logs` (billing & usage), `admin_logs` / `admin_chat_history` (call logs & chat review), `config_users` / `config_teams` / `config_invites` (users / teams / invite codes), `config_security` (security console), `config_license` (license), `config_verify` (token check), `service_configs` (external service configs), `models.py` (model management), and the page / app config writes in `content.py` |
| Either (`require_admin_or_config`) | Prompt snapshot export / import (`/v1/content/prompts/export|import`) — CLI migration scripts carry `ADMIN_TOKEN` while the Config console carries `CONFIG_TOKEN` |
| `require_super_admin` | Session user with `role=super_admin` in metadata, or a valid `ADMIN_TOKEN` as fallback |

> Note that naming and credentials do not map one-to-one: `admin_prompts`, `admin_mcp_servers`, `admin_billing`, etc. are named `admin_*` but are gated by `CONFIG_TOKEN`, and their panels render in the `/config` console. See [Admin Consoles](admin-console.md) for the full panel layout.

## Personal API keys

`api/routes/v1/api_keys.py` provides full lifecycle management for personal API keys, provided the user's `can_use_api_key` flag is true (403 otherwise):

| Endpoint | Description |
|---|---|
| `GET /v1/me/api-keys` | List the current user's keys |
| `POST /v1/me/api-keys` | Create — **the plaintext is returned only once, in the creation response**; expiry options are 7/30/90/180/365 days or never |
| `PATCH /v1/me/api-keys/{key_id}` | Enable / disable |
| `DELETE /v1/me/api-keys/{key_id}` | Revoke |

Keys look like `sk-jx-...` and can be used as a Bearer token to call business APIs **in every AUTH_MODE** (external programmatic callers usually have no cookies). Validation (enabled / not revoked / not expired / user flag still on) is centralized in `core/services/api_key_service.py::resolve_api_key`.

## Teams & invite codes (Enterprise Edition)

Teams and registration codes are multi-tenant capabilities (license feature `multi_tenancy`), administered from the Config system console:

- **Team management** (`api/routes/v1/config_teams.py`): team CRUD, member add/remove, role assignment (owner/admin/member).
- **Invite code management** (`api/routes/v1/config_invites.py`): batch generation, listing, revocation, deletion. Codes look like `JX-ABCD-2345` (`core/auth/invite.py`, with an alphabet that drops confusable characters like O/0 and I/1); default validity is `INVITE_CODE_DEFAULT_TTL_HOURS` (168 hours). Consumption uses a conditional UPDATE for concurrency safety and can pre-bind a team and role.
- **User side** (`api/routes/v1/me.py`): team owners/admins can invite and remove members directly; members can leave.

## Auditing

Key authentication events all land in the audit table (`audit_logs`): login success / failure (`auth.login.*`), wrong admin-token attempts (`admin.access_denied` / `config.access_denied`), and team / file / super_admin permission denials (`*.access_denied`, including required vs. actual permission). The audit console is Enterprise Edition — see [Admin Consoles](admin-console.md).

## Source map

| Topic | Path |
|---|---|
| Auth backend (three modes + resolution priority) | `src/backend/core/auth/backend.py` |
| Session management (Redis) | `src/backend/core/auth/session.py` |
| SSO ticket exchange | `src/backend/core/auth/sso.py`, `src/backend/api/routes/v1/auth.py` |
| Mock SSO / local login & registration page | `src/backend/api/routes/v1/mock_sso.py`, `src/backend/core/auth/mock_ticket_store.py` |
| Password hashing | `src/backend/core/auth/password.py` |
| Permission interface layer (CE/EE seam) | `src/backend/core/auth/permissions_iface.py` |
| Team roles / file permissions | `src/backend/core/auth/roles.py`, `src/backend/core/auth/team_permissions.py` |
| Project / chat-share permissions | `src/backend/core/auth/project_permissions.py`, `src/backend/core/auth/chat_share_permissions.py` |
| Administrative credential dependencies | `src/backend/api/deps.py` |
| Profile / preferences | `src/backend/api/routes/v1/users.py`, `src/backend/api/routes/v1/me.py` |
| Per-user permission flags | `src/backend/api/routes/v1/config_users.py` |
| Personal API keys | `src/backend/api/routes/v1/api_keys.py`, `src/backend/core/services/api_key_service.py` |
| Capability-center self-service (owner isolation) | `src/backend/api/routes/v1/me_capabilities.py` |
| Invite codes | `src/backend/core/auth/invite.py`, `src/backend/api/routes/v1/config_invites.py` |
| Team management | `src/backend/api/routes/v1/config_teams.py` |
| License feature guards | `src/backend/core/licensing/features.py`, `src/backend/core/licensing/deps.py` |

Further reading: [Admin Consoles](admin-console.md) · [Editions & Licensing](../editions/overview.md) · [Environment Variables](../deployment/environment-variables.md)
