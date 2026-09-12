# Windows installation and deployment

> Last updated: July 31, 2026 ｜ [简体中文](../../zh-CN/deployment/windows-deployment.md) ｜ Back to [Deployment Guide](README.md)

Windows users can run a Docker-free, desktop-managed local service or deploy the
team service through Docker Desktop and WSL2. The local option is for personal use;
the Compose option is for collaboration and production.

## Install a local service with the desktop client

Personal users on Windows x86_64 can use the NSIS desktop installer. The package
contains a single-file CE server archive that has passed the Community Edition
boundary checks. On first launch, it extracts the payload and creates an isolated
Python environment in the current user's profile. Docker Desktop, WSL2, PostgreSQL,
and Redis aren't required.

### Prerequisites

The local-service installer has the following requirements:

| Item | Requirement |
|---|---|
| Operating system | Windows 10 or Windows 11 on x86_64 |
| WebView2 | Included with Windows 11; the Tauri installer handles it when missing on Windows 10 |
| Network | Required on first install to download Python wheels and optional Node tools; the server source and web build are already in the package |
| Python | Reuses Python 3.11+ when present; otherwise, it first tries a per-user install through `winget` |
| Node.js | When Node.js 20+ is absent, the installer tries to add it through `winget`; failure doesn't block the core service |
| Disk | Keep at least 5 GB free for the Python environment, tool dependencies, data, and logs |

### Installation

Complete these steps for a personal local installation:

1. Run the HugAgentOS NSIS `.exe` installer.
2. Select **Yes** when asked whether to install the Docker-free local service.
3. Launch the desktop client and wait for resource extraction, dependency installation,
   and the health check to finish on the service setup page.
4. Sign in with the generated `admin` / `admin` account, then change the password
   when prompted.
5. Configure a working model provider in first-run setup.

If installation fails, the service setup page retains recent logs. Fix the network,
Python, or disk issue, then select **Install and start** to retry idempotently. You
don't need to reinstall the desktop client.

### Runtime and data

The local service listens only on `http://127.0.0.1:32101`; it isn't exposed to the
LAN. Exiting the desktop app stops the service process. Minimizing to the tray keeps
it running for background automations. After **Minimize to tray** is selected, the
close-confirmation window is destroyed immediately and does not reappear when the
main window is restored from the tray.

The runtime and data live under this directory:

```text
%LOCALAPPDATA%\com.hugagent.desktop\local-server\
  data\                    SQLite, storage, workspace, and persistent logs
  runtime\
    source\                CE server payload matching the desktop version
    venv\                  Isolated Python environment
    node\                  Re-creatable Node tools and browser runtime
    installed-bundle.json Installed-version marker
  logs\                    Desktop installer and service-manager logs
  server.pid               PID marker used to safely adopt/stop a process after a crash
```

When a desktop update contains a new service payload, the client upgrades `source`
and Python dependencies while preserving `data`. An interactive uninstall asks whether
to delete local-service data and defaults to **No**. Select **No** to preserve accounts,
conversations, uploads, and workspaces, or select **Yes** to remove them. Silent updates
always preserve data. The uninstaller stops the service, atomically renames the
directories selected for deletion, and lets a hidden system process clean them in the
background. The uninstall UI doesn't wait for every Python and Node file to be removed.

Use **File → Set server address…** to switch to a team server. Use
**File → Local service…** to reinstall, start, or switch back to the local service.

> **Note:** The local service shares the single-process local profile used by the
> [No-Docker quick install](quick-install.md), but optional Windows host tools can
> degrade: React site building and advanced PDF rendering remain unavailable when
> automatic Node.js installation fails, and Milvus Lite vector knowledge bases are
> not currently available on native Windows. Multi-user, production,
> high-availability, and full container sandbox use still require Docker Compose.

The Windows title bar matches the sidebar background across its full width and follows the light or dark theme. File, Edit, View, and Help use compact text aligned to the left, without back or forward arrows. The empty title area remains draggable, with minimize, maximize, and close controls on the right.

## Team deployment with Docker Desktop and WSL2

HugAgentOS uses Linux container orchestration for standard team deployments. On a
Windows host, run it through **Docker Desktop + WSL2**. Every service remains in a
Linux container, and Windows acts only as the host. The following sections cover
the differences from Linux deployment.

Outside the desktop-managed personal profile, manually running the team backend in
native Windows Python isn't supported.

### Prerequisites

| Item | Requirement |
|---|---|
| CPU architecture | x86_64 (amd64) only. Several bundled binaries and upstream sandbox images are linux/amd64; ARM Windows is not supported |
| Docker Desktop | WSL2 backend enabled, and your distro enabled under Settings → Resources → WSL Integration |
| WSL2 distro | Any mainstream Linux distro (e.g. Ubuntu 22.04+); all deployment operations happen in its bash shell |
| Compose | Docker Desktop ships the v2 plugin (`docker compose`), which is sufficient |

### Key principle: keep everything on the WSL2 native filesystem

The repository and data directories must live on WSL2's ext4 filesystem (e.g. `/home/<user>/`), **not** on a Windows drive (`/mnt/c/...`):

- `/mnt/c` goes through the 9P protocol; small-file IO is an order of magnitude slower, which hurts both the mounted backend source and sandbox storage;
- sandbox features bind-mount and whitelist-check "host absolute paths"; Windows drive paths are invalid inside containers;
- file events (inotify) on `/mnt/c` are unreliable.

```bash
# inside WSL2 bash
git clone <repo-url> ~/HugAgentOS
cd ~/HugAgentOS
```

### Line endings (CRLF)

The repository ships a `.gitattributes` that forces LF for scripts, templates, Dockerfiles, and compose files executed inside containers, so a fresh clone just works. For older clones or if you have changed git config globally, also set inside WSL2:

```bash
git config core.autocrlf input
```

> Symptom reference: a container failing with `bash\r: No such file or directory`, or nginx `[emerg]` config parse errors, means scripts/templates were converted to CRLF — re-checkout with LF.

### `.env` deltas

On top of `.env.example`, pay special attention to these variables on Windows/WSL2:

| Variable | Windows/WSL2 value |
|---|---|
| `HOST_REPO_PATH` | Absolute repo path inside WSL2, e.g. `/home/<user>/HugAgentOS` |
| `HOST_STORAGE_PATH` | Absolute path inside WSL2, e.g. `/home/<user>/hugagent-storage` (create it first) |
| `DOCKER_GID` | Run `stat -c '%g' /var/run/docker.sock` inside WSL2 and use the printed GID (under Docker Desktop it is usually not the default 999) |

Before first deployment, create the storage directory and make it writable by the in-container user (UID 1000):

```bash
mkdir -p ~/hugagent-storage
sudo chown -R 1000:1000 ~/hugagent-storage
```

### Choosing a sandbox provider

- **Use the default `script_runner`** (profile `script_runner`): a single sidecar container with no second-hop host-path forwarding; works fine on Windows.
- **Do not use `opensandbox` (EE)**: it spawns nested sandbox containers through the host `docker.sock` and requires "path inside the backend container == path as seen by the host daemon" to match exactly. Docker Desktop runs its daemon in a separate `docker-desktop` distro whose filesystem view differs from your distro, so the path whitelist checks and bind-mounts will most likely fail. This provider is only recommended on Linux hosts.

### Startup

Run every command inside WSL2 bash (`make` and `scripts/deploy/*.sh` are bash scripts; PowerShell/CMD cannot run them directly):

```bash
docker compose --profile script_runner up -d --build
```

Verify:

```bash
docker compose ps
curl -fsS http://localhost:3000/api/health
```

### Optional: mem0 memory infrastructure

The `mem0` profile (Milvus + etcd + MinIO + Neo4j) is memory-heavy. On Windows, raise the WSL2 memory cap first in `%UserProfile%\.wslconfig` (e.g. `memory=12GB`), then enable:

```bash
docker compose --profile mem0 up -d
```

### Known limitations

| Item | Notes |
|---|---|
| Manually running the team backend on native Windows | Not supported; use the desktop-managed local service for personal use |
| `opensandbox` provider (EE) | Not usable under Docker Desktop; use `script_runner` |
| ARM Windows | Not supported (amd64-only images and binaries) |
| Repo/storage on `/mnt/c` | Not supported (performance and path-semantics issues); must live on the WSL2 filesystem |
| Admin "rebuild sandbox dependencies" | Requires a correct `DOCKER_GID`; with a wrong GID the feature degrades gracefully without affecting anything else |

## Plugins and site publishing in hybrid mode

With desktop capabilities v2, both the main agent and subagents first see a plugin directory.
Calling `load_plugin` exposes that plugin's selected skills and MCP tools. Preparation still
pins selected components, sources, revisions and dependencies before execution; progressive
loading delays model exposure and MCP connections. Newly synchronized cloud plugin definitions
are verified and prepared when their components are selected for the run. Skill bodies remain
on-demand. Explicit skill, connector or plugin selections take effect immediately. Subagent
activation stays within that subagent's run. Activation rechecks account, authorization and file
integrity; ambiguous plugin names require the qualified directory identifier.

Hybrid mode hosts official sites in the cloud:

1. Write and build the project locally. For build-based projects, publish the build output.
2. Call `publish_site`. The local runtime packages that directory as tar.gz and uploads the
   actual bytes using the current account's capability credential.
3. The cloud rechecks the publishing grant and tool schema, writes files to its configured
   storage (such as OSS), and creates or updates the hosted site version.
4. The tool returns the official cloud URL. Site lists, versions, submissions and key-value
   data belong to the cloud. Include the same `site_id` when publishing an update.

Source files and local conversations stay on the computer. Publishing does not synchronize the
entire local project into a cloud personal or team workspace. Static sites upload page files;
build-based sites upload output only, not the `source_dir` source tree. Temporary local previews
remain available. Archives are limited to 40 MB; extracted sites retain the 30 MB total, 10 MB
per-file and 300-file quotas. Offline or unavailable cloud publishing produces an explicit error,
with no fallback to local hosting. Hybrid lists no longer merge historical local sites, and cloud
404 responses never retry a local site. Historical files are not automatically deleted.
Standalone local deployments retain their existing behavior.

Deployment requires the cloud backend, bundled local backend and Windows/UOS desktop client
updates. Updating only the cloud backend does not replace an old client's routing or runtime.

Publishing a local folder project resolves its bound host directory without requiring a cloud space folder. Build output must remain inside that project directory or the local workspace. Missing directories, projects owned by other users, and escaping links are rejected.


### Local site sources and editing

In hybrid mode, creating a site from the Sites panel prepares a local folder project before generation.
An existing local project is reused. Write source files in the real project directory, then publish
static page files or the build output for a React-style project. Both source and build output must
already exist inside the bound local project. Session scratch directories are not migrated.

After successful publication, the desktop persists a binding between the current cloud account, site ID,
source directory and original conversation. Edit opens the source and conversation on this computer.
Republishing uses the same site ID, preserving the URL and incrementing the cloud version.
Multiple sites in one project have separate bindings, scoped by cloud address and account.
Missing or out-of-project source directories do not enable local editing.

No legacy source-link recovery or automatic source transfer to other computers is provided.

Update both the desktop frontend and its bundled local backend. Updating only the cloud service cannot
replace an already-installed desktop client.

## Scheduled task execution location
Choose Local or Cloud and verify the time zone when creating a task. Local directory projects default to
Local; ordinary online tasks default to Cloud. Local tasks require the computer and desktop backend to be running.
Tasks that read host directories must bind an existing local project. Cloud tasks cannot directly access host paths.
Creation and edits validate resource compatibility; each execution rechecks project access and its directory binding.
Lists, details and run history retain the execution backend; local tasks display their device and project.
Existing tasks remain on their original backend and are not migrated automatically.
Authenticated cloud operation bindings and atomic receipts prevent duplicate writes; lost responses are reconciled
against cloud receipts. Upgrade the cloud backend/MCP, bundled local backend and desktop frontend together, and
apply the standard database migrations.

The scheduled-task creation form shows a Local/Cloud selector only in desktop hybrid mode. The cloud web app and cloud-only desktop show a fixed Cloud location; local-only desktop shows a fixed Local location.

## Cloud management of hybrid desktop logs

Model, chat history, tool, agent and skill log pages show both local and cloud task sections by default. The task-origin selector filters all, local or cloud tasks and persists across log menus and page reloads. Administrators can filter local records by user/device/conversation/run and open conversations or record details. Device status shows last sync, pending records, reconciliation and capture errors. Gateway error counters are process-local and reset on restart.

The desktop captures pending identifiers in the existing database transaction. A background worker uploads batches approximately every three seconds, retries offline and resumes after restart. The cloud accepts events idempotently by account, device, event and revision. Historical data is reconciled incrementally. Uploads are outside the inference wait path; local database capture still has a small cost, so absolute zero overhead is not promised.

Cloud model/tool gateways also capture server execution observations and deduplicate them with desktop context. Skill load/call and agent runtime records use existing local instrumentation; internal model reasoning is not collected. Missing provider usage remains unknown. Whitelisted chat/call data is credential-redacted and oversized content is explicitly marked truncated. Project files and attachments are not replicated.

Upgrade the cloud backend, management frontend and bundled desktop backend together, and apply normal migrations including deskobs01. Management queries belong to EE audit; CE retains receiver/model compatibility. The desktop must be signed into the matching cloud account with its backend running. Old clients do not emit these new records.

Records are eventually visible. Gateway persistence uses a bounded asynchronous queue, so a hard process exit or queue overflow can lose server observations; error counters expose failures and durable desktop uploads can supplement captured records. Management copies do not modify the existing billing ledger.


### Explicit site editing targets and skill updates

Editing works from project chats, new chats, existing chats, or site cards. In local projects,
the agent calls `list_project_sites` to discover current-account site IDs and source/output directories.
It selects according to the user's request and asks when candidates are ambiguous.
Updates explicitly pass the original `site_id` and the actual page/build output `src_dir`.
Local publishing no longer defaults to the project root or selects a site from chat metadata.
Omit the ID only for an intentional new site. Verify the returned ID, URL, version and actual homepage.

Builtin Sites version 1.2.0 upgrades existing older builtin instructions on service startup, including
global and private installations. Enabled states and connection settings are preserved; uninstalled
or user-imported plugins are untouched. Update the cloud backend and site MCP plus the local backend.
Desktop capability synchronization then downloads the new cloud skill hash and package.
Rebuilding the desktop alone does not replace installed skills in the cloud database.
Verify that a real site conversation has `list_project_sites` and loads the explicit-update instructions.


On macOS, the main interface extends to the top of the window without a separate blank title row above the content. The sidebar background continues behind the traffic lights, while its brand and buttons retain safe spacing. The collapsed sidebar is 88px wide to keep native window controls clear of content. Drag the sidebar top or empty space in the content header to move the window; double-click to toggle maximization. Standalone setup and login pages retain a top inset.


During capability synchronization, local change capture snapshots account identity when a database transaction starts, before executing SQL, so it never waits for the account lock while holding a database write lock. Repeated desktop bridge authentication leaves unchanged user profiles untouched and runs in a worker thread to keep database waits off the service event loop. After upgrading, retry previously failed synchronization without deleting the local database. The macOS capability synchronization page uses the same layout background for its top inset in both light and dark themes.
