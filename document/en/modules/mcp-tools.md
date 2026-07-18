# MCP Tool System

> Last updated: 2026-07-02

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
│  agent) │        │        │   :9103  ai_chain_information_mcp (industry, EE)│
└─────────┘        │        │   :9104  generate_chart_tool                    │
     │             │        │   :9105  report_export_mcp                      │
 MCPConnectionPool │        │   :9106  web_fetch                              │
 (core/llm/        │        │   :9107  batch_runner                           │
  mcp_pool.py)     └───┬────┘   :9108  automation_task                        │
                       │        9109–9111 reserved (former office MCPs)       │
                       │        :9112  skill_manager                          │
                       └──────────────────────────────────────────────────────┘
```

The single source of truth for port assignment is `src/backend/mcp_servers/_ports.py`: both `core/config/mcp_config.py` (which builds the backend-side `http://mcp:NNNN/mcp/` URLs) and `mcp_servers/_launcher.py` (which binds those ports inside the container) read from it.

> Historical note: the office-document MCP servers (word / excel / ppt / pdf) have been moved out of the `mcp` container entirely. That capability now ships as [agent skills](agent-skills.md) (word-editing / excel-editing / ppt-design / pdf-editing) that execute inside the sandbox container, each vendoring its own engine. Consequently `docker/Dockerfile.mcp` no longer installs LibreOffice / .NET / Node / Chromium; 9108 has been reused for automation task management, while 9109–9111 remain reserved.

## Built-in MCP servers at a glance

| Server (directory) | Port | Tools | Edition |
|---|---|---|---|
| `retrieve_dataset_content_mcp` | 9100 | `retrieve_dataset_content` / `list_datasets` / `retrieve_local_kb` | Community CE |
| `query_database_mcp` | 9101 | `query_database` | **Enterprise EE** |
| `internet_search_mcp` | 9102 | `internet_search` | Community CE |
| `ai_chain_information_mcp` | 9103 | 13 industry-chain / company-profile tools (below) | **Enterprise EE** |
| `generate_chart_tool_mcp` | 9104 | `generate_chart_tool` | Community CE |
| `report_export_mcp` | 9105 | `export_table_to_excel` | Community CE |
| `web_fetch_mcp` | 9106 | `web_fetch` | Community CE |
| `batch_runner_mcp` | 9107 | `batch_plan` | Community CE |
| `automation_task_mcp` | 9108 | `create_scheduled_task` / `list_scheduled_tasks` / `update_scheduled_task` etc. | Community CE |
| `skill_manager_mcp` | 9112 | `search_marketplace` / `install_from_marketplace` / `register_skill` / `list_my_skills` / `submit_to_marketplace` / `delete_skill` | Community CE |

> Edition boundaries follow the [open-source & commercialization plan](../editions/overview.md): the two industry servers depend on intranet-only data sources (the industry knowledge center and the data warehouse) and are Enterprise-only — the CE derivation pipeline strips their directories via `ce/manifest.yaml` and drops their `catalog.json` seeds. The remaining eight general-purpose servers all ship in the Community Edition.

### retrieve_dataset_content — knowledge-base retrieval (CE)

The retrieval entry point for knowledge-base RAG; one server exposes three tools:

- **`retrieve_dataset_content(query, dataset_id, top_k, score_threshold, search_method, reranking_enable, weights)`**: semantic/hybrid retrieval against external Dify knowledge bases;
- **`list_datasets()`**: lists every knowledge base (public + private) available to the current user, including names, descriptions and document lists, so the model can explore before retrieving;
- **`retrieve_local_kb(kb_id, query, top_k)`**: retrieval against the platform's self-hosted private knowledge bases.

It is the only **per-request** server: for each chat request the backend injects the allowed KB IDs, current user ID and reranker flag as **HTTP headers** (`X-Allowed-Dataset-Ids` / `X-Allowed-Kb-Ids` / `X-Current-User-Id` / `X-Reranker-Enabled`; see `core/llm/agent_factory.py::_apply_runtime_kb_constraints`), and the server reads them from `ctx.request_context` to enforce multi-user isolation. See the [knowledge base module](knowledge-base.md).

### query_database — data-warehouse query (Enterprise EE)

`query_database(question, employee-id)`: passes the user's complete natural-language question as a whole to the intranet data-warehouse service, which performs question decomposition, multi-table joins and NL2SQL internally and returns verifiable, precise metric values (industrial added value, growth rates, total profit, etc.). The tool description ranks it as the **highest-priority data source for precise numeric questions**. It cannot run without the intranet warehouse, hence Enterprise-only.

### internet_search — web search (CE)

`internet_search(query, max_results, topic, search_depth, include_raw_content, cn_only)`: internet retrieval backed by Tavily-style engines (configured via `TAVILY_API_KEY` / `INTERNET_SEARCH_ENGINE`), supporting general / news / finance topics and several search depths. In the tool-selection policy it is positioned as the **fallback of last resort** — used only when internal KBs, the warehouse and industry tools come up empty.

### ai_chain_information_mcp — industry knowledge center (Enterprise EE)

A grouped server that bundles 13 industry-chain and company-profile tools under one MCP server (keeping pluggability at the server level); the implementation is split across `impl_chain / impl_news / impl_latest / impl_company / impl_entity / impl_rank`:

| Tool | Purpose |
|---|---|
| `get_chain_information(chain_id)` | Industry-chain panorama report & core metrics |
| `get_industry_news(keyword, news_type, chain, region)` | Industry news feed |
| `get_latest_ai_news()` | Aggregated AI-sector headlines |
| `search_company(keyword, top_num)` | Fuzzy company search |
| `get_company_base_info(company_id)` | Basic company profile |
| `get_company_business_analysis(company_id)` | Business analysis |
| `get_company_tech_insight(company_id)` | Technology insight |
| `get_company_funding(company_id)` | Funding history |
| `get_industry_hot_companies(...)` | Trending companies ranking |
| `get_industry_hot_products(...)` | Trending products ranking |
| `get_company_hot_events(...)` | Company hot events |
| `get_product_detail(...)` | Product details |
| `get_company_risk_warning(company_id)` | Risk warnings |

Depends on the intranet "industry knowledge center" — Enterprise-only.

### generate_chart_tool — data visualization (CE)

`generate_chart_tool(data, query)`: takes JSON data plus a plotting instruction, renders line/bar/pie charts with matplotlib (the mcp container bundles WenQuanYi and FangZheng fonts for CJK rendering), saves the image as a platform artifact and returns a `file_id` / download URL. The tool description mandates fetching real data first ("never plot from thin air") and documents the standard hand-off to the sandbox (`sandbox_put_artifact` to copy the chart in before embedding it in Word/PPT).

### report_export_mcp — lightweight table export (CE)

`export_table_to_excel(markdown, title, filename)`: one-click conversion of Markdown tables already produced in the chat into a styled .xlsx download (one sheet per table). Anything needing formulas, multi-sheet models or editing existing files goes through the excel-editing skill instead.

> The server's former `export_report_to_docx` (Markdown → official-document-style Word) **MCP entry point has been retired**, superseded by the word-editing skill's `word-cli create --markdown`; the function body is kept only for selftest regression (see the header comment of `report_export_mcp/server.py`).

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

## Administrator-defined MCP servers

The admin console's MCP management maps to `api/routes/v1/admin_mcp_servers.py` (prefix `/v1/admin/mcp-servers`):

- **CRUD**: create/edit servers of any transport (`stdio` / `streamable_http` / `sse`), with `command+args` (stdio) or `url+headers` (HTTP/SSE), environment injection (`env_vars` literals + `env_inherit` from the host), icons and user-facing intros;
- **Probe-on-create**: `_probe_connectivity` performs a real connection; failures are rejected before persisting;
- **Toggle & ordering**: `POST /{id}/toggle` switches a server instantly (refreshing the catalog and the connection pool);
- **Secret masking**: the list endpoint masks secret-looking values in `env_vars`;
- **Test & reload**: `POST /{id}/test` re-probes a single server; `POST /reload-pool` hot-rebuilds the connection pool.

## User self-service MCP (capability center)

Regular users can add remote MCP servers visible **only to themselves** (`api/routes/v1/me_capabilities.py`, prefix `/v1/me`):

- `POST /v1/me/mcp-servers`: add a private remote MCP — **HTTP/SSE only**; the user entry point deliberately forbids stdio (no arbitrary command execution on the server); probe-on-create, unreachable endpoints are never persisted;
- `DELETE /v1/me/mcp-servers/{id}`: remove one's own private MCP.

Implementation reuses the same `admin_mcp_servers` table: `owner_user_id` = current user for owner isolation, auto-generated `umcp_<hex>` server IDs to avoid clashes, and `is_stable=False` to keep them out of the warmup pool. The feature is gated by the per-user `can_add_mcp` permission flag (open by default in the single-tenant Community Edition; granted per user by organisation admins in the Enterprise Edition — see [editions](../editions/overview.md)).

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
| `src/backend/core/config/mcp_config.py` | Built-in server URL builder (http://mcp:NNNN/mcp/) |
| `src/backend/core/config/catalog.json` | Capability catalog: MCP enable/disable seeds |
| `src/backend/api/routes/v1/admin_mcp_servers.py` | Admin custom-MCP API |
| `src/backend/api/routes/v1/me_capabilities.py` | User self-service private MCP / skill API |
| `docker/Dockerfile.mcp` | mcp container image (matplotlib/openpyxl/pandoc/CJK fonts) |

Related docs: [Capability catalog](catalog.md) · [Agent skills](agent-skills.md) · [Knowledge base](knowledge-base.md) · [Editions & licensing](../editions/overview.md)
