# Backend Architecture

> Last updated: 2026-07-22

The backend lives in `src/backend/` and is a cleanly layered FastAPI monolith: the API layer handles only protocol and auth, the orchestration layer turns each chat into a resumable streaming Run, `core/` holds all domain logic, and MCP tools plus the script-execution sidecar run as independent processes. This page walks the stack top-down.

## Top-Level Layout

```
src/backend/
├── api/             # FastAPI app, middleware, and CE routes
├── edition_ee/      # EE routes, License, Team/RBAC, ORM, Dify, and service implementations
├── orchestration/   # Chat orchestration: Run executor, workflow, strategy, citations, schedulers
├── core/            # Domain core: 17 submodules (auth/llm/db/ontology/services/...)
├── prompts/         # Prompt assembly runtime + file fallback texts
├── extensions/       # Bundled extensions
│   ├── skills/       # default / marketplace
│   ├── agents/       # default / marketplace
│   ├── plugins/      # default / marketplace
│   └── mcp_servers/  # MCP process implementations
├── scripts/         # Ops scripts: content export/import, entrypoint, init SQL
├── alembic/         # DB migration chain (EE main chain; CE has its own baseline, see Data Model)
└── tests/           # pytest suite
```

Dependencies are one-directional: `api → orchestration → core`; `extensions/mcp_servers` and `core/sandbox/runner` expose separate process entry points. The runner shares process-lifecycle primitives without loading backend settings or databases; the backend calls it over HTTP.

## core/ Submodules

### core/auth — authentication and permissions

| File | Responsibility |
|---|---|
| `backend.py` | Auth backend abstraction: `AUTH_MODE=mock/remote` switch |
| `password.py` | Local-account password hashing (Argon2id) |
| `roles.py` | Single source of truth for team role constants |
| `permissions_iface.py` | Permission interface layer — CE/EE seam C3; the CE tree swaps in a single-tenant implementation via overlay |
| `session.py` (Enterprise Edition, EE) | Redis-backed session management |
| `sso.py` (Enterprise Edition, EE) | Enterprise SSO ticket-exchange client |
| `invite.py` (Enterprise Edition, EE) | Invite-code generation and atomic consumption |
| `team_permissions.py` / `project_permissions.py` / `chat_share_permissions.py` (Enterprise Edition, EE) | Permission resolution for team folders / projects / in-team chat sharing |
| `mock_ticket_store.py` | Dev-only mock-SSO ticket store |

### core/llm — agents and models

| File | Responsibility |
|---|---|
| `factory/build.py` | The core factory `create_agent_executor`: assembles prompts, filters MCP servers, registers tools and skills, and produces an AgentScope 2.0 Agent |
| `chat_models.py` | Model factory (builds AgentScope ChatModels from DB model config) |
| `mcp_manager.py` / `mcp_pool.py` | MCP client pool: stable connections are reused; transient ones are closed per request |
| `tool_collector.py` | Adapts incremental `register_*` style to the 2.0 one-shot Toolkit |
| `middlewares.py` | AgentScope 2.0 middlewares (replacing 1.x hooks): pre_reply / post_acting / post_reasoning |
| `hooks.py` | Pure-logic helpers: file-context construction, per-turn state, model resolution |
| `tools/` | Built-in tool registrations: `read/write/edit/glob/grep_tool.py` (general-purpose file read/write/search tools), `sandbox_tool.py` (bash + artifact staging), `skill_tool.py` (skill loading), `pin_tool.py`, `read_artifact_tool.py`, `_tool_confirmation/` (shared tool approval and interaction) |
| `context_manager.py` / `history_summarizer.py` / `summarizer.py` | Context-window budgeting, structured history summarization, conversation summarization |
| `offloader.py` | Spills oversized tool results to the sandbox `/workspace/.offload` for read-back |
| `subagent_tool.py` | The `call_subagent` tool: lets the main agent dispatch tasks to sub-agents |
| `classifier.py` / `finish_guard.py` / `system_reminder.py` / `message_compat.py` / `workspace.py` | Conversation classification, silent-exit recovery, out-of-band system reminders, message format conversion, pin-workspace state |
| `skill_distiller.py` (Enterprise Edition, EE) | Distills conversation trajectories into skill drafts |

### core/db — data access

| File/Package | Responsibility |
|---|---|
| `engine.py` | Engine, SessionLocal, `init_db` startup table fallback |
| `models/` | ORM model package, split into 14 domain files (see [Data Model](./data-model.md)) |
| `repository/` | Repository layer: `agent/artifact/audit/catalog/chat/kb/team/user.py` |
| `model_repository.py` | Repository for model providers / role assignments |
| `edition_tables.py` | `EE_ONLY_TABLES` + `ce_create_all()` — single source of truth for the CE/EE table boundary |

### core/config — configuration and capability catalog

| File | Responsibility |
|---|---|
| `settings.py` | Centralized app settings (env-driven, including the `JX_EDITION` flag) |
| `catalog.json` + `catalog.py` | Capability-catalog single source of truth + the `is_enabled` / `get_enabled_ids` gating API |
| `catalog_loader.py` / `catalog_migration.py` / `catalog_common.py` | Catalog loading, caching, DB-override merging, shape migration |
| `catalog_resolver.py` | Unified capability resolution: request → effective skill/mcp/kb id sets |
| `mcp_config.py` | MCP server connection definitions (streamable-http URLs derived from `_ports.py`) |
| `display_names.py` / `user_intros.py` | Display names and user-facing intros for tools and MCP servers |
| `runtime_env.py` | DB-backed env lookup for services in the mcp container |
| `distillation.py` (Enterprise Edition, EE) | Skill-distillation thresholds / keywords / cron defaults |

### core/services — business service layer (59 services)

Chat domain: `chat_service` (sessions and messages), `plan_service` (plan mode), `automation_service` (scheduled tasks), `user_agent_service` (custom sub-agents).

Content domain: `artifact_service` (artifacts / MySpace resources), `kb_service` (knowledge base), `catalog_service` (catalog overrides), `prompt_version_service` (prompt version pool), `marketplace_service` (skill marketplace), `skill_icon_service`, `skill_deps_aggregator` (aggregates skill dependencies into sandbox build manifests).

User domain: `user_service`, `local_user_service` (register / login / password change), `api_key_service` (personal API keys), `user_folder_service`, `project_service` + `project_file_service` + `project_scope` (project workspaces).

Config domain: `model_config` (DB-backed model config, cached), `system_config` (service config), `mcp_service` (MCP server config), `log_service` (async observability log writer).

Ontology domain: `ontology_service` (user setting, versions, and runtime cropping) and `ontology_evolution_service` (evidence prefiltering, sanitization, human-review drafts, and inactive-version materialization); `core/ontology/` contains the four-layer schema, build gate, deterministic runtime gate, tool filter, and prompt renderer.

Enterprise Edition (EE): `team_service` / `team_folder_service` / `sso_sync` (teams and SSO sync), `distillation_service` (skill distillation), `sandbox_rebuild_service` + `cube_template_builder` (persistent-sandbox template rebuilds), `security_service` (read-only security-console aggregation).

### core/memory — the three-layer memory system

| File | Responsibility |
|---|---|
| `profile.py` | L1 profile memory: bounded markdown dossier, frozen and injected at session start |
| `service.py` / `graph.py` | L2: mem0 + Milvus procedure vectors; L3: local Neo4j graph relations (config assembly + async wrapping) |
| `pipeline.py` | Post-hoc write pipeline — all memory writes are taken off the SSE hot path |
| `extractors/` | 4 LLM extractors (identity/preference/fact/task) + `router.py` classification dispatch + `writers.py` persistence fan-out |
| `sanitizer.py` | Sensitive-data scrubbing gate (rules stored in `memory_sanitizer_rules`) |
| `context.py` | `MemoryContext` — the unified context carrier |
| `audit.py` (Enterprise Edition, EE) | Memory-operation audit trail (a no-table overlay stub in CE) |

### core/sandbox — sandbox providers

The root contains only the protocol, errors, factory, and package initializer. Implementations are grouped by provider and shared responsibility.

| File/Package | Responsibility |
|---|---|
| `protocol.py` / `errors.py` / `factory.py` | Interfaces, data contracts, exceptions, and provider selection via `SANDBOX_PROVIDER` |
| `providers/script_runner/` | HTTP client and authentication for script-runner |
| `providers/opensandbox/` | OpenSandbox provider, session creation/lifecycle, snapshots, volumes, transport, warm pool, and recovery (shared by CE/EE) |
| `providers/cube/` (Enterprise Edition, EE) | Tencent CubeSandbox (E2B-compatible MicroVM) provider |
| `providers/evaluation/` | Evaluation sandbox bindings and process limits |
| `providers/edition.py` / `providers/account_env.py` | Edition adapter and account environment assembly |
| `runner/` | Standalone execution service; Docker and desktop use `core.sandbox.runner.server:app` |
| `processes/` | Foreground/background processes, output, deadlines, cloud adapters, and thread/event-loop dispatch |
| `sessions/` | Session registry, routing, binding, keepalive, and metadata |
| `workspace/` | Local/container/desktop paths, mounts, archives, streaming writes, and temporary credential cleanup |
| `permissions/` | Local path/command permission evaluation and OS sandbox admission |
| `oslayer/` | OS isolation policy models, path rules, policy builders, and platform execution |

The runner depends only on process and path primitives that do not load backend settings or databases. OpenSandbox-specific components stay with that provider; shared processes, paths, and account environment support serve both OpenSandbox and Cube.

### core/capabilities/skills — the skill engine

| File | Responsibility |
|---|---|
| `loader.py` + `registry.py` | Multi-source skill loading and registration (SKILL.md spec, skill-creator aligned) |
| `backends/` | Storage backend abstraction: `filesystem` (extensions/skills), `database` (admin_skills table), `composite` merge |
| `selector.py` | Dynamically selects skills based on user intent |
| `skill_archive.py` | Builds and caches tar.gz archives of skill directories for fast sandbox delivery |
| `deps_detector.py` | Detects pip/npm/apt runtime dependencies from a skill's bundled files |
| `binary_files.py` / `cache_refresh.py` / `config.py` | Binary attachment support, cache invalidation after admin mutations, multi-source config |

### Remaining core submodules

| Submodule | Responsibilities and key files |
|---|---|
| `core/chat` | Workflow context assembly (`context.py`), SSE tool-log event construction (`tool_log.py`) |
| `core/files/content` | Attachment parsing (`file_parser.py`), KB document chunking/keywords/vectorization (`kb_processing.py`), upload validation (`file_validation.py`), artifact reading and summaries (`artifact_reader/refs/summary.py`), content-block import/export (`content_blocks.py`), `svg_fit.py` |
| `features/knowledge` / `edition_ee/kb` | Shared private-KB parsing and Milvus vector storage stay in `features/knowledge`; the Dify client and external retrieval adapter live only in `edition_ee/kb` (EE) |
| `core/files/artifacts` | Artifact store `store.py`: local / OSS dual mode |
| `core/infra` | Unified responses (`responses.py`), exceptions (`exceptions.py`), structured logging (`logging.py`), Redis singleton (`redis.py`), metrics (`metrics.py`), background-task registry (`runtime_state.py`), data masking (`data_masking.py`), distillation budget gates (`distillation_budget.py`, Enterprise Edition, EE) |
| `edition_ee/licensing` | Feature enums, signature verification, clock guard, seat policy, middleware, and the manager all exist only in EE; the CE tree has no License package and gets a fixed edition probe from its `api/middleware/edition.py` overlay |
| `core/files/storage` | Storage protocol `protocol.py` + factory `factory.py`; `local.py` (CE), `s3.py` / `oss.py` (Enterprise Edition, EE) |

## orchestration/ — the orchestration layer

| File | Responsibility |
|---|---|
| `chat_run_executor.py` | Decouples the AI workflow from the HTTP connection into background Runs: start, SSE following, offset-based resume, crash recovery |
| `workflow.py` | The per-turn streaming orchestration body: memory injection → agent build → stream consumption → citation extraction → meta wrap-up |
| `streaming.py` | AgentScope 2.0 streaming wrapper `StreamingAgent`, yielding normalized event chunks |
| `strategy.py` | Routing strategy: `ROUTER_STRATEGY=main_only` (default) / `llm_router` (placeholder, falls back to main) |
| `citations.py` | Tool results → `[ref:tool_name-N]` citation extraction |
| `memory_integration.py` | Non-blocking memory I/O integration off the SSE hot path |
| `followups.py` | Standalone follow-up question generator |
| `message_parser.py` | Message content extraction and parsing helpers |
| `registry.py` | Agent registry |
| `tool_payloads.py` | SSE tool_result payload builders (artifact cards, skill loading, and other special shapes) |
| `tool_callbacks.py` | Tool-call soft warnings (observation only — never blocks) |
| `subagents/plan_mode.py` | Plan-mode sub-agent: generates and executes structured plans |
| `schedulers/automation_scheduler.py` | Automation scheduler: polls the DB for due tasks and fires them |
| `schedulers/distillation_cron_scheduler.py` (Enterprise Edition, EE) | Daily skill-distillation cron |

## api/ — the API layer

### Application and middleware

`api/app.py` creates the FastAPI app and runs startup hooks serially in its lifespan: table fallback → Run recovery → stale-Run reaper → sandbox pool warm-up → page-config / prompt-version seeding → MCP catalog sync → preloading → automation and distillation schedulers → memory warm-up. Middleware lives in `api/middleware/`: `cors.py`, `logging.py` (structured request logs + trace_id), and `error_handler.py` (exceptions → unified error envelope). `api/deps.py` provides auth and user-resolution dependencies, `api/health.py` the health checks, and `core/chat/schemas.py` the shared Pydantic models.

### The router registry (CE/EE seam C1)

The full repository composes `CE_ROUTERS` in `api/routes/v1/__init__.py` with `EE_ROUTERS` in `edition_ee/routes/registry.py` (currently 36 entries each). EE entries carry a License feature bit enforced by `edition_ee/licensing/deps.py` as the second line of defense (the first is physical removal of EE files from the CE tree); the CE overlay fixes `EE_ROUTERS` to empty. Three entries — `config_verify` / `config_license` / `auth` — are explicitly exempt so login and license replacement remain reachable after expiry.

### Route file groups

CE routes live under `api/routes/v1/`; physically split commercial routes live under `edition_ee/routes/`, while remaining historical admin routes are mounted only by the EE registry and its feature guards.

| Group | Files |
|---|---|
| Chat and streaming | `chats.py` (the SSE streaming entry point), `chat_runs.py`, `chat_shares.py`, `summary.py`, `classify.py`, `memories.py` |
| Content and files | `content.py`, `file_upload.py`, `file_parse.py`, `artifacts.py`, `myspace_folders.py`, `kb.py` (+ `kb_models.py`, Pydantic models only), `projects.py` |
| Capabilities and config | `catalog.py`, `models.py`, `config.py`, `marketplace.py`, `me_capabilities.py` (self-service capability center), `ontologies.py` (user setting, version governance, evidence loop), `agents.py`, `plans.py`, `automations.py`, `meta.py` (edition / feature probe, unauthenticated) |
| Users and auth | `users.py`, `me.py`, `api_keys.py`, `mock_sso.py` (dev) |
| Content console (Enterprise Edition, EE; backend of `/admin`) | `admin_skills.py`, `admin_prompts.py`, `admin_kb.py`, `admin_agents.py`, `admin_mcp_servers.py`, `admin_marketplace.py`, `admin_skill_drafts.py`, `admin_sandbox.py`, `admin_logs.py`, `admin_usage_logs.py`, `admin_billing.py`, `admin_chat_history.py` |
| System console (Enterprise Edition, EE; backend of `/config`) | `config_users.py`, `config_teams.py`, `config_invites.py`, `config_security.py`, `config_verify.py`, `config_license.py`, `service_configs.py` |
| Other Enterprise Edition (EE) | `auth.py` (SSO ticket exchange), `audit.py`, `team_files.py`, `data_sources.py`, `db_metadata.py`, `gateway_admin.py`, `gateway_anthropic.py` |

## extensions/mcp_servers/ and sidecars

`extensions/mcp_servers/` hosts the servers: `internet_search_mcp`, `web_fetch_mcp`, `automation_task_mcp`, `skill_manager_mcp`, `retrieve_dataset_content_mcp`, plus the intranet-dependent `query_database_mcp` and `ai_chain_information_mcp` (Enterprise Edition, EE). Shared plumbing: `_launcher.py` (starts process servers using registry placement and ports), `_serve.py`, `_common.py`, `_retrieve_cleaning.py`. Word, Excel, and other document exports are handled by Agent skills inside the sandbox.

`core/sandbox/runner/server.py` is the skill-script execution sidecar: its own container, resource-limited subprocesses, and no database / Redis / API-key access.

## prompts/ — prompt assembly

`prompt_runtime.py` is the assembly entry point: active DB version (`prompt_versions` in `content_blocks`) → file fallback in `prompt_text/default/system/*.md` → minimal hardcoded fallback. `prompt_config.py` / `provider.py` provide pluggable configuration and loading; `project_section.py` and `kb_lite_section.py` render the project-mode and lightweight KB-catalog sections; `prompt_text/` also carries `code_exec` / `distillation` / `plan_mode` scenario prompts.

## Layering Principles

1. **api does protocol only**: validation, auth, envelope wrapping — no business logic;
2. **orchestration does orchestration only**: it chains domain services into streaming workflows and never touches the ORM directly;
3. **core/services is the only business entrance**: routes must not bypass the service layer to query `core/db/models` (a handful of read-only fast paths excepted);
4. **Process boundaries are failure boundaries**: MCP, script-runner, and sandboxes all run as separate processes/containers and interact with the backend only through protocol layers;
5. **CE/EE seams stay concentrated**: the router registry, `edition_tables`, `permissions_iface`, and edition middleware are the choke points; commercial implementations move under `edition_ee` instead of scattering `if edition` branches through business code.

## Related Source

| Topic | Path |
|---|---|
| App entry and startup hooks | `src/backend/api/app.py` |
| Router registry | `src/backend/api/routes/v1/__init__.py` |
| Agent factory | `src/backend/core/llm/factory/build.py` |
| Run executor / workflow | `src/backend/core/chat/runtime/`, `workflow.py` |
| Capability catalog | `src/backend/core/capabilities/catalog/` |
| Sandbox protocol | `src/backend/core/sandbox/protocol.py` |
| Memory pipeline | `src/backend/core/memory/pipeline.py` |
| EE License implementation / CE edition middleware | `src/backend/edition_ee/licensing/`, `src/backend/api/middleware/edition.py` |
| Skill engine | `src/backend/core/capabilities/skills/loader.py` |
| MCP registry and ports | `src/backend/extensions/mcp_servers/registry.py`, `_ports.py` |

The space MCP interface lives in `extensions/mcp_servers/space_files_mcp/`. The registry separates installation placement from exposure. Hybrid desktops access spaces through the capability gateway; mutations use unified approval. See [space file mounts](../deployment/space-files.md).
