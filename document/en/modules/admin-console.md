# Admin Consoles

> Last updated: 2026-06-11

HugAgentOS ships **two independent management consoles**, aimed at content operations and system administration respectively:

| Entry | Frontend | Credential | Purpose |
|---|---|---|---|
| `/admin` operations console | `src/frontend/src/AdminApp.tsx` | `ADMIN_TOKEN` | Content operations: feature updates, capability center, skills, knowledge bases, sub-agents — "what users see and use" |
| `/config` system console | `src/frontend/src/ConfigApp.tsx` | `CONFIG_TOKEN` | System administration: model / MCP / prompt configuration, users & permissions, monitoring & billing, security audit, license |

Entry routing happens in `src/frontend/src/main.tsx`: based on the `window.location.pathname` prefix it renders `AdminApp` (`/admin`), `ConfigApp` (`/config`), `ApiDocApp` (`/api-docs`), or the main app. The two consoles cross-link (the `/admin` header's "系统配置" button → `/config`; the `/config` header's "内容管理" button → `/admin`). For how the two tokens authenticate, see [Authentication & Permissions](auth.md).

The CE/EE assignment of backend admin routes has one composed source of truth — `src/backend/api/routes/v1/__init__.py` plus `src/backend/edition_ee/routes/registry.py`: the CE-derived tree **physically omits** EE route files (first line of defense), and EE deployments are additionally guarded by license features (`content_admin` / `billing` / `audit` / `multi_tenancy` / `system_config`) via `edition_ee/licensing/deps.py::requires_feature` (second line of defense). Each group below is labeled accordingly.

## /admin operations console

`AdminApp.tsx` organizes nine panels as tabs (components in `src/frontend/src/components/admin/`):

| Tab | Component | Backend routes |
|---|---|---|
| Feature updates | `UpdatesEditor` | `content.py` (`docs_updates` content block) |
| Capability center | `CapsEditor` | `content.py` (`docs_capabilities` content block) |
| Skill management | `SkillsEditor` | `admin_skills.py` |
| Pending drafts | `SkillDraftsPanel` | `admin_skill_drafts.py` |
| Sandbox dependencies | `SandboxDepsManager` | `admin_sandbox.py` |
| Knowledge base management | `KnowledgeBaseManager` | `admin_kb.py` |
| Prompt hub | `PromptHubEditor` | `content.py` (`prompt_hub` content block) |
| Sub-agents | `AdminAgentManager` | `admin_agents.py` |
| User manual | `ManualEditor` | `content.py` (manual PDF upload) |

## /config system console

`ConfigApp.tsx` organizes five menu groups in a left-hand sidebar (components in `src/frontend/src/components/config/` plus reused ones from `components/admin/`):

| Group | Panels | Backend routes |
|---|---|---|
| Basic configuration | System config / page config / app config / model management / MCP tools / prompt management | `service_configs.py`, `content.py`, `models.py`, `admin_mcp_servers.py`, `admin_prompts.py` |
| Users & permissions | User management / team management / invite codes | `config_users.py`, `config_teams.py`, `config_invites.py` |
| Data monitoring | User call logs / token billing / user chat history / tool, sub-agent & skill call logs | `admin_usage_logs.py`, `admin_billing.py`, `admin_chat_history.py`, `admin_logs.py` |
| Security | Sandbox management / audit logs / system health | `config_security.py` |
| Licensing | License | `config_license.py` |

> A naming caveat: route files like `admin_prompts`, `admin_mcp_servers`, and `admin_billing` are named `admin_*` but their auth dependency is `require_config` (`CONFIG_TOKEN`), and their panels render under `/config` — the file-name prefix does not indicate which credential applies.

## Backend admin route groups

### Skill management (admin_skills / admin_skill_drafts) (Enterprise Edition: content_admin)

`/v1/admin/skills` (`api/routes/v1/admin_skills.py`, `ADMIN_TOKEN`): full skill lifecycle — CRUD, zip upload, enable/disable, ordering, icons, dependency scan and editing, per-file read/write/delete inside a skill, forking built-in skills, import/export. Skills are stored in the `admin_skills` table; see [Agent Skills](agent-skills.md).

`/v1/admin/skill-drafts` (`api/routes/v1/admin_skill_drafts.py`, `ADMIN_TOKEN`): review of auto-distilled skill drafts — list / count / detail, edit-then-approve, reject, delete, plus a manual trigger for the daily distillation scan.

### Marketplace review (admin_marketplace) (Enterprise Edition: content_admin)

`/v1/admin/marketplace` (`api/routes/v1/admin_marketplace.py`, `ADMIN_TOKEN`) does two things:

1. **Install marketplace skills globally**: browse the marketplace listing (flagging "already installed globally"); installed skills have an empty owner, are available to everyone, and remain editable under Skill Management.
2. **Review user listing submissions**: submission list (filterable by status), detail (with SKILL.md preview), approve (publish, installable by all), reject / unpublish.

### Prompt management (admin_prompts) (Enterprise Edition: content_admin)

`/v1/admin/prompts` (`api/routes/v1/admin_prompts.py`, `CONFIG_TOKEN`) operates on two layers:

- **Parts**: the system prompt as ordered `.md` segments — list / detail / save / delete / reorder / runtime preview. DB records override filesystem fallback files; deleting a DB record restores the file version.
- **Version pool**: multi-version management for the four prompt kinds (system / code_exec / distillation / plan_mode) — CRUD, activate, and seed from filesystem. Stored in `ContentBlock(id="prompt_versions")`.

Cross-environment snapshot endpoints `GET /v1/content/prompts/export` / `POST /v1/content/prompts/import` (`content.py`) accept either `ADMIN_TOKEN` or `CONFIG_TOKEN`. See [Prompt System](prompts.md).

### MCP management (admin_mcp_servers) (Enterprise Edition: content_admin)

`/v1/admin/mcp-servers` (`api/routes/v1/admin_mcp_servers.py`, `CONFIG_TOKEN`): MCP server configuration CRUD (stored in the `admin_mcp_servers` table), enable/disable, connectivity test, and connection pool reload. See [MCP Tools](mcp-tools.md).

### Sub-agents (admin_agents) (Enterprise Edition: content_admin)

`/v1/admin/agents` (`api/routes/v1/admin_agents.py`, `ADMIN_TOKEN`): admin-owned sub-agent CRUD (visible to all users), bindable-resource listing (available-resources), enable toggle, import/export.

### Knowledge base management (admin_kb) (Enterprise Edition: content_admin)

`/v1/admin/kb` (`api/routes/v1/admin_kb.py`, `ADMIN_TOKEN`): management of self-hosted **public knowledge bases** (`KBSpace.visibility == "public"`, owned by the system owner), mirroring the user-side `kb.py` — KB CRUD, AI-generated descriptions, document upload / listing / original-file preview (Office→PDF) / deletion / reindexing, chunk preview and per-chunk editing (content / tags / questions). Public KBs are visible to all users in the capability catalog and searchable. In Dify mode, Dify datasets are shown read-only and writes return 409. See [Knowledge Base](knowledge-base.md).

### Sandbox management (admin_sandbox + config_security/sandbox)

Two complementary panels:

- `/v1/admin/sandbox` (`api/routes/v1/admin_sandbox.py`, `ADMIN_TOKEN`, **Enterprise Edition: content_admin**): aggregated skill-dependency manifest preview, sandbox image rebuild trigger + container hot-swap, rebuild history and log queries.
- `/v1/config/security/sandbox/*` (`api/routes/v1/config_security.py`, `CONFIG_TOKEN`, **Enterprise Edition: system_config**): read-only security view — sandbox overview, running instances list / detail, snapshots, rebuild history, redacted configuration. Capabilities a provider cannot support (e.g. ScriptRunner cannot enumerate instances) return `code=42210`, which the frontend renders as disabled.

See [Sandbox](sandbox.md).

### Billing reports (admin_billing / admin_usage_logs) (Enterprise Edition: billing)

`/v1/admin/billing` (`api/routes/v1/admin_billing.py`, `CONFIG_TOKEN`): billing summary statistics, per-model pricing CRUD, billing detail CSV export.

`/v1/admin/usage-logs` (`api/routes/v1/admin_usage_logs.py`, `CONFIG_TOKEN`): per-user agent call log queries (token usage, model, error status), summary statistics, distinct model names.

> Community Edition users can view their own token usage; organization-wide reports / pricing / cost export are Enterprise Edition.

### Logs & chat review (admin_logs / admin_chat_history) (Enterprise Edition: audit)

`/v1/admin/logs` (`api/routes/v1/admin_logs.py`, `CONFIG_TOKEN`): the observability log trio — tool calls, sub-agent calls, and skill calls, each with list / filters / summary / detail, plus `GET /trace/{trace_id}` to aggregate a full call chain by trace.

`/v1/admin/chat-history` (`api/routes/v1/admin_chat_history.py`, `CONFIG_TOKEN`): browse all users' chat sessions, message details (including tool-call results), user filtering, XLSX export of chat history.

There is also an API-oriented global audit query at `/v1/audit` (`api/routes/v1/audit.py`, **Enterprise Edition: audit**): audit log query / detail / CSV / JSON export / statistics; the "audit logs" panel under `/config` uses `config_security.py`'s `/v1/config/security/audit-logs*`.

### Content management (content.py) (Community Edition)

`/v1/content` (`api/routes/v1/content.py`) is one of the few admin route groups that stays in CE — branding, copy, and release notes belong to the open-source "rebrandable" experience:

| Endpoint | Credential | Description |
|---|---|---|
| `GET /docs`, `GET /docs/version` | public read | Frontend reads content blocks / lightweight version polling |
| `PUT /docs/{block_id}` | `ADMIN_TOKEN` | Write content blocks: `docs_updates` (release-note timeline), `docs_capabilities` (capability center), `prompt_hub` (prompt hub) |
| `POST /manual/upload`, `GET /manual` | `ADMIN_TOKEN` for writes | User manual PDF |
| `PUT /app_config`, `PUT /homepage_shortcuts`, `PUT /page_config`, `POST /page_config/assets/upload` | `CONFIG_TOKEN` | App config / homepage shortcuts / page branding (logo, navigation, copy) |
| `GET/POST /docs/export|import`, `GET/POST /prompts/export|import` | admin credentials | Content / prompt snapshot migration |

### Users / teams / invites / security / license management

| Route group | File | Credential | Edition | Description |
|---|---|---|---|---|
| `/v1/config/users` | `config_users.py` | `CONFIG_TOKEN` | Enterprise Edition (multi_tenancy) | User list / detail / status, password reset, deletion, and the five permission flags (app visibility, lab, API key, self-service skills, self-service MCP) — see [Authentication & Permissions](auth.md) |
| `/v1/config/teams` | `config_teams.py` | `CONFIG_TOKEN` | Enterprise Edition (multi_tenancy) | Team CRUD, member add/remove, role assignment |
| `/v1/config/invites` | `config_invites.py` | `CONFIG_TOKEN` | Enterprise Edition (multi_tenancy) | Invite code batch generation / list / revoke / delete |
| `/v1/config/security` | `config_security.py` | `CONFIG_TOKEN` | Enterprise Edition (system_config) | Security console (read-only): sandbox, audit logs, system health snapshot |
| `/v1/config/license` | `config_license.py` | `CONFIG_TOKEN` | exempt from feature guard | View license status, upload & activate a new license — **must stay reachable even when the license is invalid**, otherwise the license could never be replaced; see [License](../editions/license.md) |
| `/v1/config/verify` | `config_verify.py` | `CONFIG_TOKEN` | exempt | Token validity check before console login |

### Service configs (service_configs) (Enterprise Edition: system_config)

`/v1/service-configs` (`api/routes/v1/service_configs.py`, `CONFIG_TOKEN`): the external service configuration center — list / batch update / connectivity test / import & export for the four groups query_database, knowledge_base, industry, and file_parser. Every endpoint is an administrative write or probe with no public reads, so the whole group is EE.

## CE / EE boundary summary

Aligned with chapter 4 of the productization plan and the route registry:

- **Community Edition (CE) keeps**: `content.py` content-block management (rebrandable), `models.py` model management, and login infrastructure (`auth.py` session endpoints, mock SSO).
- **Enterprise Edition (EE)**: the full content console (skills / drafts / marketplace / KB / sub-agents / prompts / MCP / sandbox dependencies, `content_admin`), the system console (service configs + security console, `system_config`), audit & chat review (`audit`), team billing & usage (`billing`), and users / teams / invite codes (`multi_tenancy`).
- `config_license`, `config_verify`, and `auth` are explicitly exempt from the license guard so the "402 → replace license" escape hatch is always reachable.

## Source map

| Topic | Path |
|---|---|
| Frontend entry routing | `src/frontend/src/main.tsx` |
| Operations console | `src/frontend/src/AdminApp.tsx`, `src/frontend/src/components/admin/` |
| System console | `src/frontend/src/ConfigApp.tsx`, `src/frontend/src/components/config/` |
| Route registry (CE/EE single source of truth) | `src/backend/api/routes/v1/__init__.py` |
| Administrative credential dependencies | `src/backend/api/deps.py` |
| License features | `src/backend/edition_ee/licensing/features.py`, `src/backend/edition_ee/licensing/deps.py` |
| Skills / drafts / marketplace | `src/backend/api/routes/v1/admin_skills.py`, `admin_skill_drafts.py`, `admin_marketplace.py` |
| Prompts / MCP / sub-agents | `src/backend/api/routes/v1/admin_prompts.py`, `admin_mcp_servers.py`, `admin_agents.py` |
| Knowledge base / sandbox | `src/backend/edition_ee/routes/admin_kb.py`, `src/backend/api/routes/v1/admin_sandbox.py` |
| Billing / usage / logs / chat review | `src/backend/api/routes/v1/admin_billing.py`, `admin_usage_logs.py`, `admin_logs.py`, `admin_chat_history.py` |
| Content management | `src/backend/api/routes/v1/content.py` |
| Users / teams / invites / license (EE) | `src/backend/edition_ee/routes/config_users.py`, `config_teams.py`, `config_invites.py`, `config_license.py` |
| Security | `src/backend/api/routes/v1/config_security.py` |
| Service configs | `src/backend/api/routes/v1/service_configs.py` |

Further reading: [Authentication & Permissions](auth.md) · [Prompt System](prompts.md) · [Editions & Licensing](../editions/overview.md)
