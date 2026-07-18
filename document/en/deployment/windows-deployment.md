# Windows Deployment (Docker Desktop / WSL2)

> Last updated: 2026-07-16 ｜ [简体中文](../../zh-CN/deployment/windows-deployment.md) ｜ Back to [Deployment Guide](README.md)

> **When to use**: running [Docker Compose](docker-compose.md) on a **Windows host** via Docker Desktop + WSL2; this page only covers the deltas from a Linux deployment.

HugAgentOS's standard deployment is Linux container orchestration (see the Docker Compose deployment guide). On a Windows host, the recommended and only supported approach is **Docker Desktop + WSL2**: every service still runs as a Linux container; Windows is merely the host. This page lists the deltas and required configuration compared with a Linux deployment.

Running the backend directly on native Windows Python (without Docker) is **not supported** — the code has POSIX-only dependencies and container-path assumptions.

## Prerequisites

| Item | Requirement |
|---|---|
| CPU architecture | x86_64 (amd64) only. Several bundled binaries and upstream sandbox images are linux/amd64; ARM Windows is not supported |
| Docker Desktop | WSL2 backend enabled, and your distro enabled under Settings → Resources → WSL Integration |
| WSL2 distro | Any mainstream Linux distro (e.g. Ubuntu 22.04+); all deployment operations happen in its bash shell |
| Compose | Docker Desktop ships the v2 plugin (`docker compose`), which is sufficient |

## Key principle: keep everything on the WSL2 native filesystem

The repository and data directories must live on WSL2's ext4 filesystem (e.g. `/home/<user>/`), **not** on a Windows drive (`/mnt/c/...`):

- `/mnt/c` goes through the 9P protocol; small-file IO is an order of magnitude slower, which hurts both the mounted backend source and sandbox storage;
- sandbox features bind-mount and whitelist-check "host absolute paths"; Windows drive paths are invalid inside containers;
- file events (inotify) on `/mnt/c` are unreliable.

```bash
# inside WSL2 bash
git clone <repo-url> ~/HugAgentOS
cd ~/HugAgentOS
```

## Line endings (CRLF)

The repository ships a `.gitattributes` that forces LF for scripts, templates, Dockerfiles, and compose files executed inside containers, so a fresh clone just works. For older clones or if you have changed git config globally, also set inside WSL2:

```bash
git config core.autocrlf input
```

> Symptom reference: a container failing with `bash\r: No such file or directory`, or nginx `[emerg]` config parse errors, means scripts/templates were converted to CRLF — re-checkout with LF.

## `.env` deltas

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

## Choosing a sandbox provider

- **Use the default `script_runner`** (profile `script_runner`): a single sidecar container with no second-hop host-path forwarding; works fine on Windows.
- **Do not use `opensandbox` (EE)**: it spawns nested sandbox containers through the host `docker.sock` and requires "path inside the backend container == path as seen by the host daemon" to match exactly. Docker Desktop runs its daemon in a separate `docker-desktop` distro whose filesystem view differs from your distro, so the path whitelist checks and bind-mounts will most likely fail. This provider is only recommended on Linux hosts.

## Startup

Run every command inside WSL2 bash (`make` and `scripts/deploy/*.sh` are bash scripts; PowerShell/CMD cannot run them directly):

```bash
docker compose --profile script_runner up -d --build
```

Verify:

```bash
docker compose ps
curl -fsS http://localhost:3000/api/health
```

## Optional: mem0 memory infrastructure

The `mem0` profile (Milvus + etcd + MinIO + Neo4j) is memory-heavy. On Windows, raise the WSL2 memory cap first in `%UserProfile%\.wslconfig` (e.g. `memory=12GB`), then enable:

```bash
docker compose --profile mem0 up -d
```

## Known limitations

| Item | Notes |
|---|---|
| Backend on native Windows | Not supported (POSIX dependencies, container-path assumptions, docker.sock dependency) |
| `opensandbox` provider (EE) | Not usable under Docker Desktop; use `script_runner` |
| ARM Windows | Not supported (amd64-only images and binaries) |
| Repo/storage on `/mnt/c` | Not supported (performance and path-semantics issues); must live on the WSL2 filesystem |
| Admin "rebuild sandbox dependencies" | Requires a correct `DOCKER_GID`; with a wrong GID the feature degrades gracefully without affecting anything else |
