# HugAgentOS Documentation

> Last updated: 2026-06-11 | [简体中文](../zh-CN/README.md)

HugAgentOS is an enterprise-grade AI agent platform: a FastAPI backend, a React frontend, and an AgentScope 2.0 agent runtime, fully containerized with Docker and shipped in an open-core model as a **Community Edition (CE)** and an **Enterprise Edition (EE)**. This tree is the complete product documentation for operators, users, and integrators.

## Getting Started

| Document | Description |
|------|------|
| [Introduction](getting-started/introduction.md) | What it is, feature overview, CE/EE at a glance, tech stack and architecture |
| [Quick Start](getting-started/quick-start.md) | Up and running with Docker Compose in 10 minutes |

## Deployment

Start with the [Deployment Guide (choosing a method)](deployment/README.md), then dive into a specific method:

| Document | Description |
|------|------|
| [Deployment Guide · Overview](deployment/README.md) | Comparison and selection of deployment methods, post-deployment verification |
| [No-Docker Quick Install](deployment/quick-install.md) | Zero-dependency single machine: one command, SQLite + in-process fakeredis + subprocess MCP/sandbox |
| [Docker Compose Deployment](deployment/docker-compose.md) | Team/production standard: full service topology, profiles, rebuild workflows, database migrations |
| [Offline Production Deployment](deployment/offline-production.md) | Isolated environments: image tarball packaging / production-side loading / prompt snapshot migration (Enterprise Edition) |
| [Windows Deployment](deployment/windows-deployment.md) | Docker Desktop / WSL2 deltas, line endings, paths and sandbox limitations |
| [Environment Variables](deployment/environment-variables.md) | Complete variable reference (defaults / purpose / CE·EE relevance) |

## Architecture

| Document | Description |
|------|------|
| [Overview](architecture/overview.md) | Layered architecture, full request lifecycle, container topology, key design decisions |
| [Backend](architecture/backend.md) | Full `src/backend/` walkthrough: api / orchestration / 15 core submodules / router registry |
| [Frontend](architecture/frontend.md) | Five app entries, component groups, Zustand stores, hooks, build chain |
| [Data Model](architecture/data-model.md) | 44 tables by domain, dual alembic chains, CE/EE table boundary |

## Modules

| Document | Description |
|------|------|
| [Chat & Orchestration](modules/chat.md) | End-to-end chat flow, SSE events, citations, plan mode, sub-agents, stream resume |
| [Prompt System](modules/prompts.md) | Assembly precedence, version pool, prompt hub, cross-environment migration |
| [Capability Catalog](modules/catalog.md) | Catalog as single source of truth, capability gating, self-service capabilities |
| [Model Providers](modules/model-providers.md) | Provider & role system, dynamic switching, personal API keys, billing |
| [MCP Tool System](modules/mcp-tools.md) | 8 built-in servers, connection pooling, admin/user custom MCP |
| [Agent Skills](modules/agent-skills.md) | SKILL.md mechanics, bundle layering, skill marketplace, distillation |
| [Sandbox Execution](modules/sandbox.md) | Three providers, bash tools, snapshot persistence, MySpace bind-mount |
| [Memory System](modules/memory.md) | L1/L2/L3 layered memory, sanitization, audit, mem0 infrastructure |
| [Knowledge Base](modules/knowledge-base.md) | Self-hosted KB (hybrid retrieval) and Dify integration, public KB admin |
| [Object Storage](modules/storage.md) | local / s3 / oss backends, artifact store, file pipelines |
| [Projects & My Space](modules/projects-myspace.md) | Project workspaces, personal/team folders, files into context |
| [Authentication & Permissions](modules/auth.md) | Three AUTH_MODEs, SSO, permission bits, admin credentials |
| [Admin Consoles](modules/admin-console.md) | The /admin and /config consoles, 19 admin route groups |
| [Automation & Batch](modules/automation.md) | Scheduled tasks, plan mode, batch orchestration |
| [Canvas & Artifacts](modules/canvas-artifacts.md) | Univer canvas, code artifacts, artifact center, chat sharing |

## API Reference

| Document | Description |
|------|------|
| [API Overview](api/overview.md) | Response envelope, authentication, SSE protocol, full route inventory |
| [Error Codes](api/error-codes.md) | Implemented error codes, license 402, frontend handling conventions |

## Editions (Community / Enterprise)

| Document | Description |
|------|------|
| [Community vs Enterprise](editions/overview.md) | Open-core model, feature matrix, three runtime modes, upgrade paths |
| [License Mechanism](editions/license.md) | Offline signature verification, state machine, feature enforcement, issuing tool (EE) |
| [CE Build Pipeline](editions/build-ce.md) | Manifest, build_ce.py pipeline, overlay, acceptance gates |

## Development

| Document | Description |
|------|------|
| [Backend Development](development/backend.md) | In-Docker dev model, tests, migrations, layering conventions, adding routes/MCP/skills |
| [Frontend Development](development/frontend.md) | Build hot-swap, directory conventions, API call conventions, edition gating |

---

Internal design and planning documents (CE/EE split blueprints, sandbox snapshot design, etc.) live in the [docs root](../README.md).
