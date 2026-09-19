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

## Where capabilities come from in hybrid mode

In hybrid mode the agent, skill, connector and plugin lists all come from the capabilities
synced down from the current cloud account. The local service no longer ships default plugins,
and the built-in skills and connectors that ship with the package take no part either — they are
neither listed in the capability centre nor assembled, so the model cannot call them. The cloud
decides what is installed — installing, importing, uninstalling and editing display metadata all
happen on the cloud account, after which the sidebar offers a sync entry that applies them
locally when you click it. The device decides what is on — enable state and interface
contributions are recorded locally, so disabling a plugin withdraws its panels immediately.
Skills and connectors you created yourself exist only on this machine and are always kept.

Default plugins installed locally by earlier versions (scheduled tasks, skill manager, sites)
are not deleted; they simply take no part in hybrid mode — not listed, not assembled. Switch the
machine back to local-only and they work again. Local-only deployments are unaffected and still
install the default plugins on first start.

The sites plugin also needs a build template on the device (the React template and
`init-react-site.sh`). That asset follows the plugin: it is provisioned on local install, when a
cloud sync prepares the plugin, and when a package upgrade refreshes plugins already present — so
a site-building skill synced from the cloud can still build sites inside a local project, while a
machine without the plugin gets nothing. Container deployments are unaffected: the sandbox image
already carries `/opt/site-template`.

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

Editing works from project chats, new chats, existing chats, or site cards. The agent calls the
Sites plugin's `list_sites` to discover editable site IDs and source/output directories; local and
cloud modes share this one tool.
It selects according to the user's request and asks when candidates are ambiguous.
Updates explicitly pass the original `site_id` and the actual page/build output `src_dir`.
Local publishing no longer defaults to the project root or selects a site from chat metadata.
Omit the ID only for an intentional new site. Verify the returned ID, URL, version and actual homepage.

Builtin Sites version 1.3.0 upgrades existing older builtin instructions on service startup, including
global and private installations. Enabled states and connection settings are preserved; uninstalled
or user-imported plugins are untouched. Update the cloud backend and site MCP plus the local backend.
Desktop capability synchronization then downloads the new cloud skill hash and package.
Rebuilding the desktop alone does not replace installed skills in the cloud database.
Verify that a real site conversation has `list_sites` and loads the explicit-update instructions.


On macOS, the main interface extends to the top of the window without a separate blank title row above the content. The sidebar background continues behind the traffic lights, while its brand and buttons retain safe spacing. The collapsed sidebar is 88px wide to keep native window controls clear of content. Drag the sidebar top or empty space in the content header to move the window; double-click to toggle maximization. Standalone setup and login pages retain a top inset.


During capability synchronization, local change capture snapshots account identity when a database transaction starts, before executing SQL, so it never waits for the account lock while holding a database write lock. Repeated desktop bridge authentication leaves unchanged user profiles untouched and runs in a worker thread to keep database waits off the service event loop. After upgrading, retry previously failed synchronization without deleting the local database. The macOS capability synchronization page uses the same layout background for its top inset in both light and dark themes.


## File previews and opening local files

Desktop output cards show **Open** for files stored on the computer and open the saved original in the system default application. The adjacent dropdown offers **Open containing folder**. Cloud files keep **Download**. Dual mode chooses the action from the file's ownership; switching conversations does not change an open file's source. The browser application keeps downloading files.

Local image, PDF, Office and text previews route to the local service. Office files in local folder projects also support PDF previews. Path lookup uses the existing file or project access checks. Moved or deleted files and unavailable local services produce an error instead of downloading a replacement copy. Save pending spreadsheet edits before opening the file in a system application.

Update both the desktop frontend and bundled local backend for this behavior. Updating only the cloud service does not update file actions in installed clients.

New standalone conversations in hybrid mode default to local execution. You can switch to cloud before sending, and the choice is saved for that conversation. Project conversations follow project ownership; existing conversations retain their execution location.


## Canvas layout and local project delivery

Desktop platforms share the same split layout. Canvas uses the space remaining after the navigation sidebar and keeps at least 420px for the conversation. When the main area is narrower than 900px, preview covers it; use the sidebar button to return to chat. Drag the separator or use its arrow keys to resize. Long filenames, paths and model names wrap or truncate; code blocks scroll horizontally within their own bounds.

Use `pin_to_workspace(file_paths=["report.docx"])` to display an existing local project file directly. Paths may be relative to the current project or absolute within it. `sandbox_get_artifact` also registers project files by reference, without copying or uploading their contents into artifacts storage. Repeated delivery keeps the same reference; preview and native open access the original file. Scratch exports and cloud delivery retain their existing behavior.

References remain in chat history. Access rechecks project permission, the directory binding and symlink boundaries. Moved or deleted files and rebound projects become unavailable rather than falling back to an old copy. Existing artifact copies are not deleted automatically. This change requires updating the bundled desktop frontend and local backend.

Local project references read the current original contents on subsequent access. Saving in Canvas updates the original path using the existing local snapshot mechanism. Revoked folder access, deleted/rebound projects and files moved outside the project invalidate access; saving also requires folder write access.

### Office commands and preview troubleshooting

Full desktop runtimes include pinned OfficeCLI 1.0.144, available through the local `bash` tool as `officecli --version`.
The release builder downloads and verifies its SHA-256 before bundling it; first installation requires no additional download.
Native tool manifests and runtime build/smoke scripts participate in the dependency fingerprint, so changes replace old runtimes.
Full desktop runtimes also bundle Pandoc 3.11 and LibreOffice 26.2.6 in private application directories.
The builder verifies pinned upstream sizes and SHA-256 hashes and extracts the native ZIP/TAR, MSI administrative
image, or DMG without a system installation. Linux builders require dpkg-deb and LibreOffice's system libraries.
Users need no separate Pandoc or LibreOffice installation. Build and activation checks exercise a DOCX text roundtrip
and DOCX-to-PDF conversion; failures stop activation. macOS builds re-sign the nested application bundle.
Tool changes invalidate the runtime fingerprint. Full installers and extracted runtimes are larger; thin installers
do not include these tools. Each architecture still requires native build validation. Chromium remains a separate
dependency for screenshots.

A local preview 401 should be diagnosed at the desktop identity bridge, without repeatedly signing the user out of the cloud.
Preview and local-open requests both recognize the bridge identity and retain project access checks.
HTML previews show readable loading errors and run successful pages in an isolated sandbox.
These fixes require updated bundled frontend/backend resources; a cloud-only deployment cannot repair an older client.

The desktop local runner disables OfficeCLI background self-updates so its executable stays at the version verified in the bundled runtime.

### Platform compatibility for desktop updates

New clients query updates by operating system and architecture. An unpublished platform reports no available update.
Platforms may ship separately while other platforms retain their latest available release. The shared manifest
for older clients advances only when all required platforms have packages for the same version, preventing
missing-platform errors on Mac and repeated installation of older Windows packages. Automatic updates require
a signed updater archive and a manifest; uploading a DMG for manual installation alone does not enable them.

### Desktop menus and update downloads

In hybrid mode, the File menu contains New Chat, Open Folder, and Quit. Selecting a folder opens a new conversation bound to that local project. Cancelling leaves the current conversation unchanged.

Check for Updates and About show the running desktop version. A background startup check discovers updates; when one is available, the sidebar help icon becomes a download button. Clicking it shows the current and target versions and release notes. Confirmation starts a download progress card, followed by installation and automatic restart. Cancelled or failed attempts can be retried. Windows uses silent installation without an installer window; the app and progress card close when replacement begins, then the updated app opens automatically. Any required system permission prompt remains controlled by the operating system.

These interactions ship in the desktop package. Existing clients must first upgrade to a version that includes them.

Desktop update discovery uses a persistent release notification stream. Each update-server worker shares one observer that checks release-file metadata about every two seconds. A changed release notifies connected clients, which then check their platform manifest. The sidebar reuses the existing local desktop event stream: it no longer polls every two seconds and identical state does not re-render it. Idle connections use a heartbeat about every 15 seconds, with an additional manifest safety check about every 30 minutes.

Disconnected streams reconnect with backoff. Servers without notification support fall back to a check about every five minutes. Manual checks remain available. Fast notifications require both the update-server backend and the desktop package to be upgraded; a new client against an old server uses the low-frequency fallback. Discovering a release never automatically downloads or installs it.

If an older client reports that it cannot open the update progress card after confirmation, install the fixed full package over the existing installation once. This preserves accounts, conversations, and local files; uninstalling or deleting data is unnecessary. The built-in update flow works again after this upgrade.


### Local and cloud ownership of chat attachments

In hybrid mode, chat uploads follow the conversation's execution location; project conversations follow their project. Changing the location after selecting a file uploads the original file to the final backend before sending. Regular chat and plan mode use the same rules. Upload or referenced-file failures preserve the draft and attachments for retry.

Selecting a cloud My Space file for a local task downloads it with the current account and saves a local attachment, preserving the source file. Preview, download, and native open follow the file's actual ownership. This fix requires a desktop package update. For historical attachments uploaded to the wrong backend by older clients, select and send the original files again; upgrading does not automatically migrate historical attachments.


### Private Bash on Windows

Full Windows installers bundle private Bash, the MSYS runtime, and GNU file tools from
Git for Windows 2.55.0.5. Release builders verify the pinned download size and SHA-256.
Installation extracts the tools offline into `native/git-bash` in the private runtime;
users need neither Git nor WSL, and the system PATH is unchanged.
The runner invokes private `usr/bin/bash.exe` directly. A damaged declared private
runtime fails explicitly instead of falling back to a developer's system Git.

Before packaging and activation, the runtime smoke test executes Bash with a PATH
excluding system Git. It exercises Chinese/space-containing directories, the
`find/sort/head/cut` pipeline, `grep` searches, and common file commands. Failure
blocks activation. The dependency fingerprint changes, so upgrades replace old
runtimes that lack Bash. Other platforms retain their system Bash; thin cloud-only
packages do not carry this runtime.

Rebuild and distribute the full Windows client to deliver this fix. Updating only
the cloud backend cannot repair existing desktop installations.

### Multiple windows

Choose File → New Window or press Ctrl+Shift+N to open another desktop window.
Windows share authentication, configuration, and the local service, while conversation navigation
is independent. Closing one window keeps other visible windows running; the final window uses
the existing close preference.


Chats without a bound project can also publish and edit sites. Source and output directories must remain inside that chat’s durable `.sessions/<chat-hash>/` workspace; publication records are stored on the original chat. Edit reopens that chat without creating a project or switching to a new session directory. Republishing retains the original `site_id`. Editing is unavailable after an account switch, missing source files, or loss of the original chat. Sites whose source binding was not saved by an older client require verification of the original files before restoring the binding; upgrading does not recreate missing source.
