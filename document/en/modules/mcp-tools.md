# MCP Tool System

> Last updated: 2026-08-11

HugAgentOS's tool ecosystem is built on [MCP (Model Context Protocol)](https://modelcontextprotocol.io): every category of external capability (internet search, web fetching, database queries, chart generation, ...) is an independent MCP server, all running inside a dedicated `mcp` container that the backend reaches over the streamable-http transport. This design has three payoffs:

- **Server-level pluggability** — enabling/disabling a capability is one catalog entry or one toggle in the admin console, never a code change;
- **Failure isolation** — a crashed tool process is restarted by the launcher without touching the backend;
- **An open ecosystem** — administrators can plug in any third-party MCP server (stdio / HTTP / SSE), and end users can self-serve private remote MCP servers.

## Architecture

```
                       ┌──────────────────── mcp container (docker/Dockerfile.mcp) ──┐
                       │  mcp_servers._launcher (one child process per server)│
┌─────────┐  HTTP  ┌───┴────┐   :9100  retrieve_dataset_content (KB retrieval)│
│ backend │───────▶│streama-│   :9101  query_database (data warehouse, EE)    │
│ (FastAPI│        │ble-http│   :9102  internet_search                        │
│  agent) │        │        │   :9103  unified Industry Knowledge Center (EE)│
└─────────┘        │        │   :9104  generate_chart_tool                    │
     │             │        │   :9105  reserved (former report export MCP)   │
 MCPConnectionPool │        │   :9106  web_fetch                              │
 (core/llm/        │        │   :9107  batch_runner                           │
  mcp_pool.py)     └───┬────┘   :9108  automation_task (automation plugin)    │
                       │        9109–9111 reserved (former office MCPs)       │
                       │        :9112  skill_manager                          │
                       └──────────────────────────────────────────────────────┘
```

The single source of truth for port assignment is `src/backend/mcp_servers/_ports.py`: both `core/config/mcp_config.py` (which builds the backend-side `http://mcp:NNNN/mcp/` URLs) and `mcp_servers/_launcher.py` (which binds those ports inside the container) read from it.

> Historical note: office-document editing and export (word / excel / ppt /
> pdf) have moved out of the `mcp` container into
> [agent skills](agent-skills.md) (word-editing / excel-editing / ppt-design /
> pdf-editing), whose engines run inside the sandbox. `report_export_mcp` is
> retired, and port 9105 remains reserved. The automation plugin provides port
> 9108, while ports 9109–9111 remain reserved.

## Built-in MCP servers at a glance

| Server (directory) | Port | Tools | Edition |
|---|---|---|---|
| `retrieve_dataset_content_mcp` | 9100 | `retrieve_dataset_content` / `list_datasets` / `retrieve_local_kb` | Community CE |
| `query_database_mcp` | 9101 | `query_database` | **Enterprise EE** |
| `internet_search_mcp` | 9102 | `internet_search` | Community CE |
| `ai_chain_information_mcp` | 9103 | 27 industry, enterprise, news, policy, report, patent, and technology tools | **Enterprise EE (Industry Knowledge Center plugin)** |
| `generate_chart_tool_mcp` | 9104 | `generate_chart_tool` | Community CE |
| `web_fetch_mcp` | 9106 | `web_fetch` | Community CE |
| `batch_runner_mcp` | 9107 | `batch_plan` | Community CE |
| `automation_task_mcp` | 9108 | `create_scheduled_task` / `list_scheduled_tasks` / `update_scheduled_task` etc. | Community CE (automation plugin) |
| `skill_manager_mcp` | 9112 | `search_marketplace` / `install_from_marketplace` / `register_skill` / `list_my_skills` / `submit_to_marketplace` / `delete_skill` | Community CE |

> Edition boundaries follow the
> [open-source and commercialization plan](../editions/overview.md). The
> Industry Knowledge Center plugin and data-warehouse query depend on industry
> data sources and are Enterprise-only. The CE derivation pipeline physically
> removes the plugin's one self-contained MCP and 14 Skills through
> `ce/manifest.yaml`. General-purpose tools ship in Community Edition.

### retrieve_dataset_content — knowledge-base retrieval (CE)

The retrieval entry point for knowledge-base RAG; one server exposes three tools:

- **`retrieve_dataset_content(query, dataset_id, top_k, score_threshold, search_method, reranking_enable, weights)`**: semantic/hybrid retrieval against external Dify knowledge bases;
- **`list_datasets()`**: lists every knowledge base (public + private) available to the current user, including names, descriptions and document lists, so the model can explore before retrieving;
- **`retrieve_local_kb(kb_id, query, top_k)`**: retrieval against the platform's self-hosted private knowledge bases.

It is the only **per-request** server: for each chat request the backend injects the allowed KB IDs, current user ID and reranker flag as **HTTP headers** (`X-Allowed-Dataset-Ids` / `X-Allowed-Kb-Ids` / `X-Current-User-Id` / `X-Reranker-Enabled`; see `core/llm/agent_factory.py::_apply_runtime_kb_constraints`), and the server reads them from `ctx.request_context` to enforce multi-user isolation. See the [knowledge base module](knowledge-base.md).

### query_database — data-warehouse query (Enterprise EE)

`query_database(question, employee-id)`: passes the user's complete natural-language question as a whole to the intranet data-warehouse service, which performs question decomposition, multi-table joins and NL2SQL internally and returns verifiable, precise metric values (industrial added value, growth rates, total profit, etc.). The tool description ranks it as the **highest-priority data source for precise numeric questions**. It cannot run without the intranet warehouse, hence Enterprise-only.

### internet_search — web search (CE)

`internet_search(query, max_results, topic, search_depth, include_raw_content,
cn_only)` selects Tavily, Baidu, or LangSearch through
`INTERNET_SEARCH_ENGINE` and reads only the API key for the selected engine.
All three providers return normalized `title / url / content` fields. Only
Tavily natively supports `topic`, `search_depth`, and raw-page content.
LangSearch maps its generated summary to `content`. The agent uses this tool
only as a fallback when internal knowledge bases, the warehouse, and industry
tools return no results.

### Industry Knowledge Center plugin (Enterprise EE)

The Industry Knowledge Center is now a native marketplace plugin instead of an
unremovable static MCP. One installation adds one MCP server and 14 workflow
Skills. Enablement, updates, and removal follow the plugin lifecycle. On an EE
upgrade, the backend installs the plugin once and removes the old static MCP
row. If an administrator later uninstalls it, startup doesn't restore it.

The unified server exposes 27 high-value tools:

| Component | Tools | Capabilities |
|---|---:|---|
| `ai_chain_information_mcp` (9103) | 27 | 13 mature compatibility tools plus 14 workflows for chain discovery/briefing/competitiveness, enterprise screening/evaluation, policies, reports, patents, and technology roadmaps |

The unified MCP package owns its explicit tool functions, detailed Chinese
descriptions, fixed endpoint composition, and package-local `common.py`. It
doesn't rely on a centralized dynamic registry or a root-level industry
runtime module. It resolves `industry.url` and `industry.auth_token` through the existing
runtime configuration service. Administrators keep managing these values in
**System Configuration** or the plugin's administrator configuration panel.
The plugin declares no `required_secrets`, so regular users don't enter a URL
or token during installation.

Each new tool exposes flat parameters and a detailed agent-visible description
covering its use cases, inputs, output panels, and selection against adjacent
tools. The 14 new workflows return only `结果` (business results), optional
`未获取内容` (unavailable panels), and `结果说明` when truncation occurs. The
13 mature tools retain their established business response shapes so the
industry-chain Canvas, news lists, and company-profile renderers remain
compatible. Neither group exposes endpoint paths, HTTP status, or execution
counts to the agent's answer context.
Multi-endpoint workflows remain concurrent and preserve partial success under
a 25,000-character business-data limit.

The internal audit still records all 240 endpoints discovered in the web app,
but capability inventory, arbitrary endpoint invocation, region trees, generic
entity resolution, personal libraries, collections/subscriptions, report
workbenches, and upstream write operations are no longer exposed to agents.

### generate_chart_tool — data visualization (CE)

`generate_chart_tool(data, query)`: takes JSON data plus a plotting instruction, renders line/bar/pie charts with matplotlib (the mcp container bundles WenQuanYi and FangZheng fonts for CJK rendering), saves the image as a platform artifact and returns a `file_id` / download URL. The tool description mandates fetching real data first ("never plot from thin air") and documents the standard hand-off to the sandbox (`sandbox_put_artifact` to copy the chart in before embedding it in Word/PPT).

### web_fetch — web page fetching (CE)

`web_fetch(url, extractMode, maxChars)`: fetches a URL and extracts its main content in `text` / `markdown` / `html` mode. The canonical pairing is "`internet_search` for URLs, then `web_fetch` for full text"; several search-oriented marketplace skills also use it to hit specialised search-engine URLs.

### batch_runner — batch execution planner (CE)

`batch_plan(instruction, file_ids, text_items, chat_id)`: detects "do the same thing to each item in a set" intents (enumerated objects / uploaded Excel rows / multiple documents), produces a confirmable **execution plan** with a prompt template and placeholders, then immediately ends the turn — the frontend pops a confirmation dialog, the user reviews/edits the template, and the backend executes item by item with live streaming. See the [automation module](automation.md).

### automation_task — scheduled task management (CE)

Lets the agent maintain the current user's automations from a conversation: `create_scheduled_task` creates a task, `list_scheduled_tasks` / `get_scheduled_task` inspect tasks, `update_scheduled_task` changes the Cron expression, prompt and status, and `pause_scheduled_task` / `resume_scheduled_task` / `delete_scheduled_task` handle lifecycle actions. Identity is injected through the `X-Current-User-Id` request header, so the server only operates on the current user's tasks.

### skill_manager — skill management (CE)

Supports the capability center and skill-management plugins: `search_marketplace` searches the marketplace, `install_from_marketplace` installs a marketplace skill, `register_skill` registers a personal skill from an uploaded package, `list_my_skills` lists the current user's skills, `submit_to_marketplace` files a review submission, and `delete_skill` removes a personal skill. The service layer reuses skill permission checks and owner isolation in both CE and EE.

## A uniform server layout

Every built-in server follows the same directory convention:

```
mcp_servers/<name>_mcp/
├── server.py        # FastMCP instance + @mcp.tool() thin shims (arg tolerance, stdout→stderr)
├── impl.py          # business logic (lazily imported from server.py to keep startup light)
├── _selftest.py     # offline self-check: module imports, tool signatures
└── README.md        # run/debug notes
```

The shared layer (root of `mcp_servers/`):

| File | Responsibility |
|---|---|
| `_serve.py` | The common `main()` entry: `run(mcp, default_port)` picks stdio (local-debug default) or streamable-http (in-container) from `--transport`; HTTP mode binds `0.0.0.0` and disables DNS-rebinding protection (private Docker network) |
| `_launcher.py` | The mcp container's CMD: spawns one streamable-http child per server, prefixes stdout/stderr with `[server]`, restarts crashes with exponential backoff, and exits non-zero if any child crashes more than 5 times within 60s (Docker then restarts the whole container) |
| `_ports.py` | The single source of truth for server_id → port, plus the `package_name()` mapping |
| `_common.py` | Shared helpers |

Two iron rules: **stdout is reserved for the MCP protocol** (business logs go to stderr; server.py wraps calls in `contextlib.redirect_stdout` as a backstop), and **be tolerant of malformed LLM-generated arguments** (e.g. auto-unpacking when a dict lands in a string parameter).

## Backend client: connection pool & bare-name restoration

The backend connects through AgentScope 2.0's `MCPClient`, centred on two files:

- **`core/llm/mcp_pool.py` — `MCPConnectionPool`** (process-level singleton): at startup `warmup_mcp_tools()` reads all enabled server configs from the DB and pre-connects. Pooling semantics under 2.0:
  - **stdio servers marked `is_stable=true`** keep their connection across requests (saving the 1–7 s subprocess cold start);
  - **HTTP servers are never pooled** — 2.0's stateful HTTP client is bound to its asyncio task, and reuse across requests triggers cancel-scope crashes, so each request gets a fresh `is_stateful=False` connection;
  - per-request servers (KB retrieval with user headers) connect on demand and are closed via `close_transient()` when the request ends.
- **`core/llm/mcp_manager.py` — `BareNameMCPClient`**: AgentScope 2.0 rewrites tool names to `mcp__<server>__<tool>`; this subclass restores the server-side bare name (`internet_search`, not `mcp__internet_search__internet_search`) so the display-name mapping (`core/config/display_names.py`), [citation extraction](chat.md) keyed by tool name, and frontend icon rendering all keep working as in 1.x.

The 2.0 `Toolkit` is constructed once, in `core/llm/agent_factory.py`: `Toolkit(tools=[...], mcps=clients)`.

## Registration: DB-driven config + catalog gating

The source of truth for MCP server configuration is the `admin_mcp_servers` table (ORM: `core/db/models.py::AdminMcpServer`), read through `core/services/mcp_service.py::McpServerConfigService` with a 30-second TTL cache, in a dict format compatible with the legacy `MCP_SERVERS` (`transport / command / args / env / url / headers / is_stable`). `core/config/mcp_config.py` remains as the URL builder for the built-in servers (first-deployment seeds).

Whether a server is **visible to the model** additionally passes through [catalog](catalog.md) gating: each entry in the `mcp` section of `core/config/catalog.json` corresponds to a server_id, and a server whose `is_enabled(id, "mcp_server")` is false will not be registered with the agent even if connected.

## System-config administrator-defined MCP servers

The `/config` system console's **MCP Tools → MCP Server Management** view maps to `api/routes/v1/admin_mcp_servers.py` (prefix `/v1/admin/mcp-servers`) and uses `CONFIG_TOKEN` or the `can_system_config` capability:

- **CRUD**: create/edit servers of any transport (`stdio` / `streamable_http` / `sse`), with `command+args` (stdio) or `url+headers` (HTTP/SSE), environment injection (`env_vars` literals + `env_inherit` from the host), icons and user-facing intros;
- **Probe-on-create**: `_probe_connectivity` performs a real connection; failures are rejected before persisting;
- **Toggle & ordering**: `POST /{id}/toggle` switches a server instantly (refreshing the catalog and the connection pool);
- **Secret protection**: HTTP header values are encrypted at rest and returned as `***`; secret-looking `env_vars` values are masked as well;
- **Test & reload**: `POST /{id}/test` re-probes a single server; `POST /reload-pool` hot-rebuilds the connection pool.
- **Move to MCP Marketplace**: creates a credential-free marketplace snapshot
  from an existing remote HTTP/SSE MCP and disables the original global
  instance. The service then leaves the MCP server list and can be reviewed,
  edited, and installed globally from the marketplace. A token already
  configured on the source remains encrypted server-side as an
  administrator-managed credential for authorised installers; neither the
  marketplace record nor frontend responses contain its value.

Plugin-provided MCPs, such as the automation plugin's scheduled-task MCP, don't
appear in MCP server management. Their enablement and removal follow the plugin
lifecycle and are handled in plugin management.

## User self-service MCP (capability center)

Regular users can add remote MCP servers visible **only to themselves** (`api/routes/v1/me_capabilities.py`, prefix `/v1/me`):

- `POST /v1/me/mcp-servers`: add a private remote MCP over public HTTP or HTTPS using HTTP/SSE transports; the user entry point deliberately forbids stdio (no arbitrary command execution on the server), and DNS/IP validation blocks localhost, private, link-local, and reserved addresses; probe-on-create means unreachable endpoints are never persisted. HTTP is plaintext, so HTTPS remains recommended for production;
- `DELETE /v1/me/mcp-servers/{id}`: remove one's own private MCP.

Implementation reuses the same `admin_mcp_servers` table: `owner_user_id` = current user for owner isolation, auto-generated `umcp_<hex>` server IDs to avoid clashes, and `is_stable=False` to keep them out of the warmup pool. HTTP header values are encrypted with Fernet at rest and decrypted only at runtime. The feature is gated by the per-user `can_add_mcp` permission flag (open by default in the single-tenant Community Edition; granted per user by organisation admins in the Enterprise Edition — see [editions](../editions/overview.md)).

## MCP marketplace

System marketplace governance lives at **`/config → MCP Tools`**. Matching Skill Management, the MCP server toolbar exposes two modal actions—MCP Marketplace and Listing Reviews—without another nested management tab; `/admin` has no duplicate entry. It lists and globally installs entries, reviews submissions, manages visibility, revalidates remote tools, suspends unsafe listings, and removes entries. It uses `CONFIG_TOKEN` or `can_system_config`.

The capability center's MCP marketplace uses the same listing-visibility model as the skill, sub-agent, and plugin marketplaces, including public listings and grants to selected users, teams, or roles. A marketplace listing stores only a **credential-free, reviewed version snapshot**. An administrator-published listing may retain administrator-managed credentials on its source `admin_mcp_servers` row; during installation the backend copies them into the user's private instance under encryption, without putting credential values in marketplace tables or frontend responses:

- Users can inspect tool names, input schemas, and risk level, then install a listing as a private MCP. If the marketplace auth policy is administrator-managed and all token fields are configured, the UI asks for no token. With installer-managed credentials, each user supplies a token or completes OAuth during installation.
- Users can submit an already connected private MCP, track pending/approved/rejected state, and withdraw it before review. Instances installed from the MCP marketplace don't expose the submission action, and the backend rejects duplicate listing attempts.
- When creating a remote HTTP/SSE MCP in Config, administrators can check “Require Token/Auth” and select Token or OAuth. A token may be entered centrally or left blank: a centrally supplied token makes user installation credential-free, while a blank token requires every user to enter one; OAuth is completed by each user. If the first connection is unauthorized because no administrator credential was supplied, the listing can still be created and uses `per_install` tool discovery after user authentication. Remote MCPs are not globally active until installed from the marketplace. StdIO MCPs remain locally managed and must use the plugin marketplace for cross-environment distribution.
- Administrators can edit a listing's name, summary, user introduction, category, tags, icon, and authentication policy. The policy explicitly selects no authentication, installer-provided credentials, or administrator-managed credentials. Administrator tokens are maintained only under Edit; the marketplace no longer exposes an “Update Global Credentials” action. Presentation changes and administrator-token updates propagate to existing installations. The reviewed endpoint, tool schemas, risk report, and version number remain version snapshots.
- Admin controls cover listing/delisting, user/team/role visibility, manual revalidation, soft deletion, and a security suspension kill switch. Suspension immediately disables every installation derived from that listing, and lifting it restores those installations.
- Tool names and descriptions associated with deletion, execution, or mutation produce medium/high risk reports. High-risk installations require explicit confirmation.
- By default, the backend reconnects to each remote MCP every six hours and compares its tool-snapshot hash. Drift moves the listing to `changed` and pauses new installations until review. Configure the period with `MCP_MARKET_REVALIDATE_INTERVAL`.

A fresh deployment seeds five **platform-curated templates**. They appear in the marketplace but are never installed or enabled automatically:

| Listing | Install-time input | Notes |
|---|---|---|
| Amap MCP | Amap Web Service API Key | The encrypted key is injected into the `key` URL query parameter only at runtime; the marketplace URL never contains it. |
| Metaso Search MCP | Metaso API Key | Runtime configuration automatically builds `Authorization: Bearer …`. |
| GitHub MCP | Fine-grained PAT or OAuth App sign-in | GitHub's official remote MCP does not support DCR; OAuth requires a registered Client ID/Secret. |
| GitLab MCP | Browser OAuth (recommended) or an access token with `mcp` scope | GitLab's official MCP is Beta; browser sign-in supports DCR, PKCE, and refresh tokens. Regular PATs may not connect. |
| Alibaba Cloud Observability MCP | A configured personal SSE endpoint | Configure AK/SK first in ModelScope Hosted, Function Compute, or another protected environment. The platform does not collect the cloud AK/SK directly. |

Curated templates use `per_install` discovery: marketplace details show representative official capabilities, while installation connects with that user's credential, discovers the concrete tool schemas, reassesses risk, and saves the result only on the user's private MCP. Templates with write operations, such as GitHub and GitLab, are pre-classified as high risk and require explicit confirmation.

### General authentication contract

Each marketplace version declares one or more methods in `auth_config`: `none`, `token`, or `oauth2`. `credential_mode` selects `installer` (each user) or `admin` (centrally managed) credentials. `auth_schema` only describes install-time fields injected into headers, query parameters, or a personal endpoint, and fields may be limited to specific methods. Older entries infer their effective policy from the authentication fields and administrator source credentials for backward compatibility.

When an administrator chooses centrally managed credentials during creation or marketplace editing, the backend verifies that the source server holds every required field. List and detail responses expose only `credentials_managed_by_admin=true`; installation reuses and re-encrypts those values server-side, never returning token contents. Selecting installer-provided credentials prevents marketplace installation from reusing the source token even when that token remains available for revalidation. Community submissions and platform-curated templates continue to require independent installer authentication and never reuse publisher credentials.

OAuth remote MCPs use protected-resource and authorization-server metadata discovery, Authorization Code + PKCE, state validation, RFC 8707 resource indicators, and DCR when available. Services without DCR collect a registered Client ID/Secret. Public HTTP and HTTPS are accepted for the MCP endpoint, authorization metadata, and callback base; HTTP exposes authorization codes and tokens in plaintext and is intended only for controlled test environments. Access tokens, refresh tokens, client metadata, and expiry are stored only as an encrypted bundle on the concrete installation and are refreshed at runtime; they never enter marketplace snapshots.

Same-origin deployments automatically use `/api/v1/mcp-market/oauth/callback` on the browser origin. Cross-origin frontend/backend deployments must set `MCP_OAUTH_PUBLIC_BASE_URL` to the browser-reachable API base so callback URLs are never reflected from an untrusted Host header.

User APIs live under `/v1/mcp-market` and cover browsing, details, private installation, submission, and withdrawal. For compatibility with existing clients, system-config management APIs retain the `/v1/admin/mcp-market` prefix, but they belong to `/config` authorization (`CONFIG_TOKEN` / `can_system_config`) and cover publishing, global installation, review, visibility, revalidation, suspension, and removal. To avoid remote-code-execution exposure, the marketplace accepts only remote `streamable_http` / `sse` MCP servers. Stdio capabilities should be distributed through reviewed plugins, where the plugin installation lifecycle controls their code and dependencies.

### Data boundaries

The marketplace uses four dedicated tables: `mcp_market_items` for listing metadata, `mcp_market_versions` for immutable tool snapshots, risk reports, and credential-free auth contracts, `mcp_market_submissions` for review snapshots, and `mcp_market_installations` to link versions to concrete instances. Headers, tokens, OAuth bundles, query keys, and personal endpoints never enter marketplace records. Community listings and curated templates authenticate each installer independently. Administrator-published token listings may read encrypted managed credentials from their source `admin_mcp_servers` row and re-encrypt them into the concrete installation, without ever exposing them to the frontend.

The user marketplace, authentication, and runtime refresh live in `core/` and CE routes. `/config` routes and `components/admin` remain EE-registered and are physically removed by the CE derivation manifest. The main tree uses the `mcpmkt01`–`mcpmkt04retire` migration chain; CE uses independent `ce_0003` and `ce_0004` migrations for the same core tables and retirement cleanup without importing `edition_ee`.

## Local debugging

Every server can run standalone outside the container (stdio transport by default):

```bash
# Run a single server over stdio (pairs with MCP Inspector etc.)
PYTHONPATH=src/backend python -m mcp_servers.internet_search_mcp.server

# Run over streamable-http (mimicking the in-container form)
PYTHONPATH=src/backend python -m mcp_servers.internet_search_mcp.server \
  --transport streamable-http --port 9102

# Offline self-check (imports, tool signatures)
PYTHONPATH=src/backend python -m mcp_servers.internet_search_mcp._selftest

# In-container liveness probe (lowest port started by the launcher)
curl -fsS http://localhost:9100/mcp/
```

Rebuild the container after changing MCP code:

```bash
docker-compose up -d --build mcp
```

## Source map

| Path | Description |
|---|---|
| `src/backend/mcp_servers/<name>_mcp/` | Individual MCP servers (server.py / impl / _selftest) |
| `src/backend/mcp_servers/_launcher.py` | mcp container entry: multi-process spawn + crash restart |
| `src/backend/mcp_servers/_serve.py` | Unified stdio / streamable-http entry point |
| `src/backend/mcp_servers/_ports.py` | Single source of truth for server_id → port |
| `src/backend/core/llm/mcp_pool.py` | MCP connection pool (stdio pooled / HTTP per-request) |
| `src/backend/core/llm/mcp_manager.py` | MCPClient construction + bare tool-name restoration |
| `src/backend/core/services/mcp_service.py` | DB-driven server config service (30 s cache) |
| `src/backend/core/services/mcp_marketplace_service.py` | Marketplace publishing, review, installation, visibility, and security controls |
| `src/backend/core/services/mcp_oauth_service.py` | OAuth 2.1 login, SDK metadata discovery, encrypted token storage, and refresh |
| `src/backend/core/services/mcp_marketplace_monitor.py` | Periodic remote tool-snapshot revalidation and drift monitoring |
| `src/backend/core/config/mcp_config.py` | Built-in server URL builder (http://mcp:NNNN/mcp/) |
| `src/backend/core/config/catalog.json` | Capability catalog: MCP enable/disable seeds |
| `src/backend/api/routes/v1/admin_mcp_servers.py` | Admin custom-MCP API |
| `src/backend/api/routes/v1/me_capabilities.py` | User self-service private MCP / skill API |
| `src/backend/api/routes/v1/mcp_marketplace.py` | User-facing MCP marketplace API |
| `src/backend/api/routes/v1/admin_mcp_marketplace.py` | Admin MCP marketplace and review API |
| `docker/Dockerfile.mcp` | mcp container image (MCP runtime, plotting dependencies, and CJK fonts) |

Related docs: [Capability catalog](catalog.md) · [Agent skills](agent-skills.md) · [Knowledge base](knowledge-base.md) · [Editions & licensing](../editions/overview.md)
