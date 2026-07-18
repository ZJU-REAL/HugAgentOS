# Data Model Overview

> Last updated: 2026-06-11

The data-access layer lives in `src/backend/core/db/`: ORM models are split by domain into the `models/` package (11 domain files, all re-exported verbatim from `models/__init__.py`, so the legacy `from core.db.models import X` style still works), the repository layer sits in `repository/`, and the engine and sessions in `engine.py`. Development uses SQLite, production PostgreSQL — `models/__init__.py` defines two dialect-aware types shared by all models: `JSONType` (automatically upgraded to JSONB on PostgreSQL) and `INETType` (INET on PostgreSQL).

```
core/db/
├── engine.py            # Engine, SessionLocal, init_db startup table fallback
├── models/              # ORM model package (11 domain files)
│   ├── identity.py      # Users, teams, folders, API keys
│   ├── chat.py          # Sessions, messages, runs, feedback, sandbox snapshots
│   ├── project.py       # Project workspaces
│   ├── knowledge.py     # Knowledge base, catalog overrides
│   ├── artifact.py      # Artifacts, content blocks
│   ├── config.py        # Model providers, system config, pricing
│   ├── admin.py         # Console assets: skills, prompts, MCP, marketplace
│   ├── agent.py         # Sub-agents, plan mode
│   ├── automation.py    # Scheduled tasks, distillation, batch plans
│   ├── logs.py          # Tool/sub-agent/skill call logs, audit
│   └── memory.py        # Profile memory, memory audit, sanitizer rules
├── repository/          # Repository layer: agent/artifact/audit/catalog/chat/kb/team/user
├── model_repository.py  # Repository for model providers / role assignments
└── edition_tables.py    # Single source of truth for the CE/EE table boundary
```

## Table Groups

Tables marked "(Enterprise Edition, EE)" belong to the `EE_ONLY_TABLES` set and are not created in the Community Edition (see the boundary section below).

### Users and teams (models/identity.py)

| Table | Purpose |
|---|---|
| `users_shadow` | User shadow table: master record for locally registered or user-center-synced users; the `metadata` JSONB holds per-user switches |
| `local_users` | Local-account sensitive data (Argon2id password hash, status, contacts), 1:1 with users_shadow |
| `user_folders` | MySpace personal folder tree (NULL parent = root) |
| `user_api_keys` | Personal API keys — call the agent over HTTP as the user |
| `invite_codes` (Enterprise Edition, EE) | Invite codes: single-use, may pre-bind team and role |
| `teams` (Enterprise Edition, EE) | Teams; may also be auto-created from external SSO departments (source=sso_auto) |
| `team_members` (Enterprise Edition, EE) | Team membership (users N:M teams, with roles) |
| `team_folders` (Enterprise Edition, EE) | Team folder tree |

### Chat (models/chat.py)

| Table | Purpose |
|---|---|
| `chat_sessions` | Session master table (title, mode flags, project mount, share scope) |
| `chat_session_user_states` | Per-user session state (pin / favorite) |
| `chat_messages` | Messages: role, content, tool-call JSON, extra data |
| `chat_runs` | Streaming runs: decouple AI tasks from the HTTP connection; support resume and crash recovery |
| `message_feedback` | Likes/dislikes with optional comments |
| `chat_sandbox_snapshots` | Per-chat persistent-sandbox snapshot pointers (environment restore with OpenSandbox) |

### Projects, artifacts, and content blocks (models/project.py, models/artifact.py)

| Table | Purpose |
|---|---|
| `projects` | Projects (workspaces; personal / team kinds; team columns stay NULL in CE) |
| `project_favorites` | Project stars (per user, not visible to others) |
| `artifacts` | Artifact registry: uploaded files and AI-generated files (reports, charts) alike |
| `content_blocks` | Editable content-block KV: release notes, capability-center copy, the **prompt version pool** (`id=prompt_versions`), the prompt hub (`id=prompt_hub`), etc. |

### Knowledge base (models/knowledge.py)

| Table | Purpose |
|---|---|
| `kb_spaces` | Knowledge-base spaces |
| `kb_documents` | KB documents (upload, parse, index status) |
| `kb_chunks` | Parent-chunk storage (context tier of parent-child retrieval; child vectors live in Milvus) |
| `catalog_overrides` | Runtime enable/disable overrides layered over catalog.json |

### Models and system config (models/config.py)

| Table | Purpose |
|---|---|
| `model_providers` | OpenAI-compatible model endpoints (DB-backed model config) |
| `model_role_assignments` | Role → model mapping (main / summarizer / router, at most one per role) |
| `system_configs` | External service config KV (data warehouse, KB, industry APIs, file parser) |
| `model_pricing` (Enterprise Edition, EE) | Model pricing for token billing |

### Skills and MCP (models/admin.py)

| Table | Purpose |
|---|---|
| `admin_skills` | DB-backed skills (admin global skills + user private skills, owner-isolated) |
| `admin_prompt_parts` | DB-backed prompt parts (override filesystem prompts; read at runtime in CE too, hence not EE-only) |
| `admin_mcp_servers` | DB-backed MCP server configs (including user self-service remote HTTP/SSE MCPs) |
| `marketplace_submissions` | User requests to list a private skill on the marketplace (the submission endpoint is kept in CE) |
| `marketplace_visibility_grants` (EE) | Marketplace item visibility whitelist (shared by the skill/plugin/sub-agent marketplaces; grants by user/team/role) |
| `admin_skill_drafts` (Enterprise Edition, EE) | Auto-distilled candidate skill drafts pending admin review |
| `sandbox_rebuilds` (Enterprise Edition, EE) | Records of admin-triggered sandbox image rebuilds |

### Agents and plans (models/agent.py)

| Table | Purpose |
|---|---|
| `user_agents` | Custom sub-agents (admin- or user-created, bound to skills / MCP / KB) |
| `plans` / `plan_steps` | Plan-mode plans and steps (expected skills / agents, execution status) |

### Automation and batch (models/automation.py)

| Table | Purpose |
|---|---|
| `scheduled_tasks` | Automation scheduled tasks (cron expressions, retry policy) |
| `scheduled_task_runs` | Scheduled-task run history |
| `batch_plans` | Batch execution plans (created by the batch_plan MCP tool, executed by the BatchOrchestrator after confirmation) |
| `distillation_runs` (Enterprise Edition, EE) | Skill-distillation queue + audit (at most one row per chat_id) |

### Memory (models/memory.py)

| Table | Purpose |
|---|---|
| `profile_memory` | L1 profile memory: a bounded markdown dossier frozen and injected at session start (L2/L3 vectors and graphs live in Milvus / Neo4j, not in the relational DB) |
| `memory_sanitizer_rules` | Runtime-added/disabled sensitive-word scrubbing rules (read in CE too, hence not EE-only) |
| `memory_audit` (Enterprise Edition, EE) | Full audit trail of memory reads/writes/deletes/rejected writes |

### Logs and audit (models/logs.py)

| Table | Purpose |
|---|---|
| `tool_call_logs` | One row per MCP / built-in tool execution |
| `subagent_call_logs` | Complete execution records of sub-agents / plan steps |
| `skill_call_logs` | Skill triggers (view / run_script / auto_load) |
| `audit_logs` (Enterprise Edition, EE) | User-facing audit of critical operations |

## Alembic Migrations

- **EE main chain**: 53 migrations under `src/backend/alembic/versions/`, evolving from the initial schema (including structural moves such as MCP-to-streamable-http and the retirement of the office MCPs in favor of skills). Common commands: `alembic upgrade head`, `make migrate-new msg="..."` (autogenerate is driven by `core/db/models` metadata);
- **Startup fallback**: the lifespan hook `_startup_ensure_tables` in `api/app.py` calls `core/db/engine.py::init_db`, which idempotently fills in missing tables for the SQLite dev database;
- **Independent CE chain**: the CE derived tree excludes the entire main chain; the overlay supplies a single baseline, `ce/overlay/src/backend/alembic/versions/ce_0001_initial.py` — `create_all` from SQLAlchemy metadata filtered by `EE_ONLY_TABLES`, dialect-aware (works on both SQLite and PostgreSQL). Subsequent CE schema evolution appends regular migrations on that chain.

## The CE/EE Table Boundary (core/db/edition_tables.py)

The `core.db.models` package is shared by both editions (EE model class definitions are harmless), but CE must not create empty EE-only tables. `EE_ONLY_TABLES` is the single source of truth for this boundary — 18 tables:

```
teams · team_members · team_folders · invite_codes        # multi-tenancy / SSO / invites
roles · role_assignments                                  # organization role model
kb_grants                                                 # per-user / per-team KB grants
audit_logs · memory_audit                                 # audit (the CE memory audit is a stub — no table)
model_pricing                                             # billing
data_sources · ds_table_meta · ds_column_meta · ds_golden_sql # data sources / metadata governance
gateway_virtual_keys                                      # external model-gateway virtual key mirror
sandbox_rebuilds · admin_skill_drafts · distillation_runs # sandbox rebuilds / skill distillation
```

`ce_create_all(bind)` creates every non-EE table on a **cloned MetaData**: cross-boundary foreign keys from CE tables into EE tables (e.g. `projects/artifacts → teams/team_folders`, scheme D3 "keep the column, always NULL") would make PostgreSQL fail because the referenced tables don't exist — so those constraints are stripped on the clone (columns kept, the original metadata untouched, ORM mappings unaffected). Both table-creation entry points filter identically from the same source: the CE branch of `init_db` (filtering only when `JX_EDITION=ce`) and the CE migration baseline `ce_0001`. Maintenance rule: any new EE-only model must be added to `EE_ONLY_TABLES`; set membership is asserted against the real metadata table names at startup, so a renamed model cannot silently degrade the boundary into create-everything.

A few tables that *look* EE but are required by CE are deliberately excluded from the set: `admin_prompt_parts` (read by the prompt runtime), `memory_sanitizer_rules` (queried unconditionally by the scrubbing gate), `admin_skills` / `admin_mcp_servers` (personal self-service capabilities, owner-isolated), and `marketplace_submissions` (CE keeps the submission endpoint).

## Related Source

| Topic | Path |
|---|---|
| ORM model package | `src/backend/core/db/models/` |
| Engine and startup table creation | `src/backend/core/db/engine.py` |
| Repository layer | `src/backend/core/db/repository/` |
| CE/EE table boundary | `src/backend/core/db/edition_tables.py` |
| EE migration chain | `src/backend/alembic/versions/` |
| CE migration baseline | `ce/overlay/src/backend/alembic/versions/ce_0001_initial.py` |
| Prompt version pool service | `src/backend/core/services/prompt_version_service.py` |
| Authoritative edition boundary | [Editions](../editions/overview.md) |
