# No-Docker Quick Install (Single Machine)

> Last updated: July 20, 2026 ｜ [简体中文](../../zh-CN/deployment/quick-install.md) ｜ Back to [Deployment Guide](README.md)

The simplest way to deploy, aimed at **personal single-machine trials** and **development experience**: one command installs everything, a terminal wizard sets the admin account and configures the model, then a single process starts the server and opens the browser. Zero **Docker, PostgreSQL, and Redis**.

Technical shape: a single uvicorn process (serving both the frontend static assets and the API) + SQLite + in-process fakeredis + subprocess MCP / sandbox. All data lives under `~/.hugagent/`.

> ⚠️ **Positioning**: this is a **single-process, single-user** form built for personal trials and development. It is **not suitable for multi-user collaboration or production** — use [Docker Compose](docker-compose.md) for those. The two forms coexist and do not affect each other.

## When to use it

| Good for | Not for |
|---|---|
| Quickly trying it out on your own machine | Multi-user / team collaboration (in-memory sessions, single-writer SQLite) |
| Running the whole stack cheaply during development | Production (no container isolation, no HA) |
| No Docker environment, just want a taste | Persistent sandboxes, L2/L3 memory, and other heavy capabilities |

## Prerequisites

| Item | Requirement |
|---|---|
| OS | Linux / macOS (on Windows, run inside WSL2 — see [Windows Deployment](windows-deployment.md)) |
| Python | ≥ 3.11 |
| Node.js | ≥ 20 (the public installer builds the frontend locally) |
| Rust and Cargo | Required on Linux without a compatible prebuilt `ripgrep` wheel, including x86_64 systems with glibc earlier than 2.39 |
| Network | Access to the configured LLM API endpoint |

## Install

Run the public installer from any directory:

```bash
curl -fsSL https://raw.githubusercontent.com/ZJU-REAL/HugAgentOS/main/install.sh | bash
```

The installer will:

1. Verify Python ≥ 3.11, Node.js ≥ 20, npm, Git, and Rust when the Linux platform must build `ripgrep` from source;
2. Clone or fast-forward HugAgentOS at `~/.hugagent/source`;
3. Detect optional LibreOffice and, when it is missing, explain the unavailable features and ask whether to install it; skipping it or a failed install doesn't block the remaining features;
4. Create a virtual environment at `~/.hugagent/venv` (using [uv](https://github.com/astral-sh/uv) when available, or `python -m venv` otherwise), and rebuild an incomplete environment left by an interrupted run;
5. Install `requirements.txt`, the `hugagent` console command, the built-in Agent Skills Python and Node.js dependencies, and optional local knowledge-base dependencies;
6. Build the frontend at `src/frontend/dist`;
7. Enter the interactive first-run wizard.

> Add the command to your PATH for daily use: `export PATH="$HOME/.hugagent/venv/bin:$PATH"`.

## First-run wizard (onboard)

The wizard runs entirely in the terminal, in order:

**Step 1 · Admin account** — a fresh CE data directory creates exactly one local administrator. The initial username and password are both `admin`, and the password must be changed on first sign-in. The login page has no registration function, and the backend rejects registration requests as well.

**Step 2 · Model** — pick a preset provider (DeepSeek / OpenAI / Moonshot / Qwen / Ollama) or a custom OpenAI-compatible endpoint, and fill in `base_url` / model name / `api_key` / context window. The context window defaults to a conservative 32,768 tokens and is stored as `context_length`; set it to the model endpoint's real supported value. The wizard **tests connectivity once** (a real call to `/chat/completions`) and reports failure immediately so you can reconfigure. The configured model is assigned to every chat role (main agent, summarizer, follow-up, planning, code execution, etc.).

The chat model is assigned to all 7 chat roles at once. Two more model types can be configured separately (both skippable):

**Step 2b (optional) · Index / embedding model** — powers the **self-built vector knowledge base** retrieval and L2 memory vectorization. Provide an embedding endpoint (`base_url` / model name / `api_key`); press Enter to skip (the KB is then unavailable; everything else is unaffected). The KB's vector store uses embedded **Milvus Lite** (a single file at `~/.hugagent/milvus.db`, no server required).

**Step 2c (optional) · Reranker model** — re-ranks KB hybrid-search results for sharper retrieval. Provide a reranker endpoint (`base_url` / model name / `api_key`); press Enter to skip (retrieval still works, just without re-ranking).

> HugAgentOS has 9 model roles: 7 chat roles (main agent / summarizer / follow-up / memory / chart / planning / code execution — all share the chat model above) + embedding + reranker. Onboard covers all three types; after logging in you can also assign a different model per role under Settings → System → Model Services.

**Step 3 · Plugins** — pick which built-in plugins to install (comma-separated indices / `all` / `none`; Enter installs the ★ recommended set). Recommended: `automation` (scheduled tasks), `skill-manager` (skill authoring), `sites` (conversational site-building). Installing `sites` also provisions the React site-building template. You can add or remove plugins from the plugin market later.

**Step 4 (optional) · File parser** — parsing uploaded PDFs / scanned documents needs an external parser service (MinerU-compatible); enter its API URL to enable it (written to `file_parser.api_url`), or press Enter to skip. Excel / CSV / PPTX / text parse in-process and need none of this.

**Step 5 (optional) · Internet search** — agent web search needs a search-engine key: choose `tavily` (default, get a key at [tavily.com](https://tavily.com)) or `baidu` (Qianfan AppBuilder) and enter the API key, or press Enter to skip. You can also configure it later under Settings → System → Service Config.

At the end the wizard prints a **host-capability summary** (whether Node.js, pandoc, and LibreOffice are present, gating React site-building, Word conversion, and PPT/Word online previews), then starts the server and opens `http://127.0.0.1:3001/`.

> **Warning:** The server listens on `127.0.0.1` by default. If the server must
> accept remote connections, run
> `hugagent serve --host 0.0.0.0 --port 3001 --no-browser`, and configure a
> strong administrator password, a firewall, and HTTPS first. Don't expose the
> service directly on an untrusted network.

### Non-interactive install (automation / CI)

`onboard` accepts bypass flags for scripted installs:

```bash
hugagent onboard \
  --username admin --password '<strong-password>' \
  --model-base-url https://api.deepseek.com/v1 \
  --model-api-key '<your-key>' --model-name deepseek-chat \
  --model-context-length 32768 \
  --embed-base-url https://<embed>/v1 --embed-model bge-m3 --embed-api-key '<key>' \  # optional, index/embedding model
  --reranker-base-url https://<rerank> --reranker-model bge-reranker --reranker-api-key '<key>' \  # optional, reranker model
  --plugins automation,skill-manager,sites \  # comma-separated slugs / all / none / default
  --file-parser-url http://<parser-service>/parse \  # optional, PDF/document parsing
  --no-serve            # do not auto-start the server after init
# optional: --no-test skips all model connectivity checks
```

## Daily use

```bash
hugagent            # initialized → start server and open browser; not initialized → enter the wizard
hugagent serve      # start explicitly (--host to change bind address, --port to change port)
hugagent onboard    # re-run the wizard / change configuration
hugagent doctor     # environment self-check (Python version, port availability, data dir, frontend build, deps, …)
```

## Data directory

All state lives under `~/.hugagent/` (override the location with the `HUGAGENT_HOME` environment variable):

| Path | Contents |
|---|---|
| `data.db` | SQLite database (business data, accounts, model config, system config, …) |
| `storage/` | Local file storage (My Space, artifacts, etc.) |
| `workspace/` | Sandbox working directory (where code execution writes files, replacing the in-container `/workspace`) |
| `venv/` | The virtualenv created by the installer |
| `node/` | Local Node.js packages and Chromium used by PPT and PDF Agent Skills |
| `logs/` | Backend logs |

> To uninstall, delete the `~/.hugagent/` directory (this removes all data).

## Capability boundaries

The no-Docker single-machine mode is built to be lightweight. Here is how it differs from the Compose form, grouped as "works out of the box / needs extra conditions / unavailable."

**Works out of the box**
- **Core chat + ReAct tool orchestration + plan mode + reconnect replay + citations.**
- **Code execution (bash / Python)**: the sandbox runs as a host subprocess (no container isolation), backed by a restricted environment, execution timeouts, and process-group cleanup; file tools (read/write/edit) and artifact staging (`sandbox_put/get_artifact`) all land under `~/.hugagent/workspace/`. The trust boundary is "a user running their own assistant on their own machine," different from a multi-tenant server.
- **Built-in skills** (the 5 word / excel / ppt / pdf editing skills): synced into the workspace at install time so the sandbox can run their scripts directly.
- **Built-in tool MCPs**: internet search / web fetch / batch execution / KB retrieval, etc. — the servers run fine (some need a configured external service or key to return data, see below).
- **Data visualization (charts)**: the installer installs matplotlib; works once present.
- **Projects / My Space / artifacts / data canvas / scheduled automations (created & fired from the UI) / docs & prompts**: all work on SQLite + local storage.
- **Self-built vector knowledge base**: backed by embedded **Milvus Lite** (a single file, no server), **dense-only** retrieval; requires configuring an embedding model during onboarding. For stronger hybrid retrieval, point `MILVUS_URL` at a real Milvus server (switches back automatically).

- **Automation / skill-authoring / site-building plugin capabilities**: `automation` / `skill-manager` / `sites` are **plugins** — install them with one keystroke in onboard Step 3 (or add/remove them later from the plugin market); once installed their MCP is auto-reachable locally (`http://mcp:*` hostnames are rewritten to `127.0.0.1`).

**Needs extra conditions**
- **React project build for conversational site-building**: supported once the `sites` plugin is installed — onboard provisions the React template into `~/.hugagent/site-template/` and runs `npm install` on first build. **Requires host Node.js ≥ 20 + npm**; without it only hand-written static sites are possible. The build chain's `/workspace` paths are parameterized to the local workspace (static sites match the Docker form).
- **Office document conversion and preview**: PPT/Word online previews and Office-to-PDF conversion require LibreOffice. When it is missing, the one-command installer explains the impact and asks whether to install it. Skipping it doesn't affect document generation, downloads, or other core features. For non-interactive installs, set `HUGAGENT_INSTALL_LIBREOFFICE=1` to install it automatically or `0` to skip it explicitly. Other Word conversions also use `pandoc`, and Excel read/write still uses openpyxl.
- **PDF / Word file parsing on upload**: PDF needs an external parser service (set its API URL in onboard Step 4, or `FILE_PARSER_API_URL`); Word needs host `pandoc` / `libreoffice`. Excel / CSV / PPTX / text parse in-process and work out of the box.
- **L2 vector memory**: off by default; can be enabled experimentally over the same Milvus Lite with `MEM0_ENABLED=true`.

**Unavailable**
- **L3 graph memory**: needs Neo4j, no embedded substitute.
- **Persistent sandbox / online sandbox-dependency rebuild**: depend on Docker; unavailable in local mode (degrades gracefully, no impact on the rest).

**Other**
- **Single user / single process**: sessions live in memory, so a process restart requires logging in again; SQLite does not support concurrent multi-worker writes — **do not** start with `--workers>1`.
- **Upgrade limitation**: SQLite uses `create_all`, which only creates missing tables and does not alter existing table structure; a cross-version column change requires exporting data and rebuilding.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Startup reports the port is in use | `hugagent serve --port <other-port>`; or run `hugagent doctor` first to check |
| The page shows JSON instead of the app | Frontend not built: `cd src/frontend && npm run build`, or set `FRONTEND_DIST_DIR` to a built `dist` |
| Login reports the model is unavailable | Re-run `hugagent onboard` to reconfigure the model (the wizard tests connectivity) |
| Want to switch model / change config | Re-run `hugagent onboard`, or log in and adjust under Settings → System → Model Services / Service Config |
| PPT/Word preview reports that LibreOffice isn't installed | Re-run the one-command installer and choose to install it when prompted. On Debian/Ubuntu, you can instead run `sudo apt-get update && sudo apt-get install -y libreoffice-impress libreoffice-writer libreoffice-calc`, then restart HugAgentOS. |
| Skill execution repeatedly reports `fork: Resource temporarily unavailable` | Stop the current service, rerun the public installer to upgrade, and start `hugagent` again. If an older version left child processes behind, inspect processes owned by the current user and, when needed, sign out of the login session before retrying. |
| Is the environment ready | `hugagent doctor` runs a one-shot self-check |

## Related source

| Feature | File |
|---|---|
| Installer | `install.sh` |
| CLI (onboard / serve / doctor) | `src/backend/cli.py` |
| Local-mode switch | `src/backend/core/config/settings.py` (`DeploySettings`, `DEPLOY_PROFILE=local`) |
| Frontend static hosting + `/api` bridge | `src/backend/api/local_hosting.py` |
| MCP / sandbox subprocess supervision | `src/backend/orchestration/local_subprocess.py` |
| In-process fakeredis | `src/backend/core/infra/redis.py` (`REDIS_URL=memory://`) |
| Built-in MCP catalog seed | `src/backend/core/services/mcp_service.py` (`seed_builtin_mcp_servers_if_empty`) |
| Environment variables | [environment-variables.md](environment-variables.md) (the "No-Docker local mode" section) |
