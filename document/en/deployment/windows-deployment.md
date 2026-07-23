# Windows installation and deployment

> Last updated: July 23, 2026 ｜ [简体中文](../../zh-CN/deployment/windows-deployment.md) ｜ Back to [Deployment Guide](README.md)

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
it running for background automations.

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
