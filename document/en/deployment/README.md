# Deployment Guide

> Last updated: July 21, 2026 ｜ [简体中文](../../zh-CN/deployment/README.md)

HugAgentOS supports several deployment methods, from "zero-dependency single-machine trial" to "team production" to "air-gapped offline delivery." This page helps you **pick the right one**; each method's full steps live in its own document.

## Choosing a deployment method

| Method | Best for | Docker | Database | Multi-user | Doc |
|---|---|---|---|---|---|
| **Windows desktop one-click install** | Personal Windows use; install the local service with the desktop client | Not required | SQLite | No (single user) | [windows-deployment.md](windows-deployment.md) |
| **No-Docker quick install** | Personal single-machine trial, development experience; one command and you're running | Not required | SQLite | No (single user) | [quick-install.md](quick-install.md) |
| **Docker Compose** | The standard form for teams / production — multi-user, full features | Required | PostgreSQL | Yes | [docker-compose.md](docker-compose.md) |
| **Offline production (Enterprise Edition)** | Air-gapped environments (government intranets); image tarball offline delivery | Required | PostgreSQL | Yes | [offline-production.md](offline-production.md) |

Cross-platform and reference:

| Doc | Description |
|---|---|
| [Windows Deployment](windows-deployment.md) | Installing the desktop-managed local service, or running Compose through Docker Desktop and WSL2 |
| [Environment Variables](environment-variables.md) | Complete variable reference (defaults / purpose / CE·EE relevance) |

## One-line comparison

- **Windows desktop one-click install**: run the NSIS installer and select
  **Install local service**. The first launch creates an isolated Python environment,
  starts a loopback-only service, and opens sign-in without Docker or WSL2. Data stays
  under the current user's `%LOCALAPPDATA%` directory. This is a single-process,
  single-user deployment.
- **No-Docker quick install**: the fastest command-line path on Linux and macOS. Run `curl -fsSL https://raw.githubusercontent.com/ZJU-REAL/HugAgentOS/main/install.sh | bash`; the installer creates the admin account, configures the model, starts the server, and opens the browser. Data lives under `~/.hugagent/`. **Single process, single user** — ideal for personal trials and development, not for multi-user or production.
- **Docker Compose**: the recommended standard deployment. All services are orchestrated by one `docker-compose.yml` (PostgreSQL + Redis + backend + MCP + frontend + sandbox), supporting multi-user, persistent sandboxes, layered memory, and every other capability.
- **Offline production (EE)**: for isolated environments that cannot pull images online — build image tarballs on a connected machine, copy them to production, `docker load` + `compose up`. Part of the Enterprise Edition delivery scope.

> For capability differences between the Community and Enterprise editions, see [Edition Comparison](../editions/overview.md).

## Post-deployment verification

Whichever method you use, confirm the backend is ready with the health check after startup:

```bash
# No-Docker quick install (default port 3001)
curl -fsS http://127.0.0.1:3001/api/health

# Docker Compose (default frontend port 3002, nginx reverse-proxies /api)
curl -fsS http://localhost:3002/api/health
```

A `{"status":"healthy",...}` response means the backend is up; then open the corresponding address in a browser and log in with the admin account.
