# HugAgentOS Documentation

> Last updated: 2026-10-23 | [简体中文](../zh-CN/README.md)

HugAgentOS is an open-source, self-hosted agent harness: your request keeps running in the cloud
even after you close your computer. On top of it, HugAgentOS provides an always-on Personal Agent
and runs as HugAgentOS Cloud (on your server) or HugAgentOS Local (on your desktop). It ships as an
open-source Community Edition (CE) and an Enterprise Edition (EE). This tree is the complete
product documentation for operators, users, and integrators.

## Getting Started

| Document | Description |
|------|------|
| [Introduction](getting-started/introduction.md) | The durable agent, runtime forms, feature overview, tech stack and architecture |
| [Quick Start](getting-started/quick-start.md) | Choose HugAgentOS Cloud, the desktop client or the one-command installer, and enable Personal Agent |
| [Build a Domain Ontology Quickly](getting-started/domain-ontology-quickstart.md) | Validate, publish, test, and bind tools with a minimal Domain Pack |

## Deployment

Start with the [Deployment Guide (choosing a method)](deployment/README.md), then dive into a specific method:

| Document | Description |
|------|------|
| [Deployment Guide · Overview](deployment/README.md) | Comparison and selection of deployment methods, post-deployment verification |
| [Docker Compose Deployment](deployment/docker-compose.md) | The standard HugAgentOS Cloud setup: service topology, profiles, rebuild workflows, database migrations |
| [Desktop Local Mode](deployment/desktop-local-mode.md) | HugAgentOS Local: private runtime, local-only / cloud-only / local + cloud, upgrades and rollback |
| [Local Project Workspace](deployment/local-project-workspace.md) | Execution directory and permission rules for bound local projects |
| [Local and Cloud Skills and Plugins](deployment/local-capability-management.md) | Where skills and plugins come from and how they are managed in hybrid mode |
| [No-Docker Quick Install](deployment/quick-install.md) | Single machine: SQLite + in-process state + subprocess MCP / sandbox |
| [Windows Deployment](deployment/windows-deployment.md) | Desktop-hosted single-machine service or Docker Desktop + WSL2 |
| [macOS Signing and Upgrades](deployment/macos-signing.md) | Desktop signing, notarization and upgrades |
| [My Space File Mounts](deployment/space-files.md) | The file directories shared by the web app and sandboxes |
| [Environment Variables](deployment/environment-variables.md) | Complete variable reference (defaults / purpose / CE·EE relevance) |

## Architecture

| Document | Description |
|------|------|
| [Overview](architecture/overview.md) | Layered architecture, ontology control plane, full request lifecycle, container topology |
| [Backend](architecture/backend.md) | `src/backend/` walkthrough and router registry |
| [Frontend](architecture/frontend.md) | App entries, component groups, Zustand stores, module pages and the app center |
| [Data Model](architecture/data-model.md) | Tables by domain, migration chains, CE/EE table boundary |

## Modules

**Harness and long-running work**

| Document | Description |
|------|------|
| [Chat & Orchestration](modules/chat.md) | Runs decoupled from connections, stream resume, mid-run steering, per-step context restore and compaction, plan mode, sub-agents |
| [Personal Agent](modules/personal-agent.md) | The 24/7 personal agent: background tasks, permanent cloud computer, schedules and heartbeats, IM channels, approvals, file memory and Dreaming |
| [Autonomous Loop](modules/autonomous-loop.md) | Goals that survive across turns, maker ≠ checker, stall guards, resumable runs |
| [Workflow Background Jobs](modules/workflow-jobs.md) | Managed sandbox processes, lease takeover, derived main agents |
| [Automation & Plan Mode](modules/automation.md) | Scheduled tasks and plan mode |

**Capabilities and extensions**

| Document | Description |
|------|------|
| [MCP Tool System](modules/mcp-tools.md) | Built-in and plugin servers, connection pooling, custom MCP |
| [Agent Skills](modules/agent-skills.md) | SKILL.md mechanics, bundle layering, skill marketplace |
| [Browser Automation Plugin](modules/browser-automation.md) | Browse pages, operate forms and test web pages |
| [Capability Catalog](modules/catalog.md) | Catalog as single source of truth, capability gating, self-service capabilities |
| [Sandbox Execution](modules/sandbox.md) | Providers, Bash tools, snapshot persistence, My Space mounts |
| [Model Providers](modules/model-providers.md) | Provider and role system, dynamic switching, personal API keys |
| [Prompt System](modules/prompts.md) | Assembly precedence, version pool, cross-environment migration |

**Knowledge, memory and governance**

| Document | Description |
|------|------|
| [Memory System](modules/memory.md) | L1/L2/L3 layered memory, durable outbox, procedural learning, sanitization and audit |
| [Domain Ontology Governance and Validation](modules/ontology-harness.md) | Domain Packs, tool gates, pre-delivery review, human review of evolution drafts |
| [Knowledge Base](modules/knowledge-base.md) | Self-hosted knowledge bases (hybrid retrieval) and external sources |
| [Knowledge Base Wiki](modules/knowledge-base-wiki.md) | Extracted entities and concepts as interlinked pages and a concept graph |

**Workspaces and artifacts**

| Document | Description |
|------|------|
| [Projects & My Space](modules/projects-myspace.md) | Project workspaces, personal folders, online cloud document editing, files into context |
| [Canvas & Artifacts](modules/canvas-artifacts.md) | Univer canvas, code artifacts, artifact center, chat sharing |
| [Sites](modules/sites.md) | Build sites in chat and host them on the platform |
| [Application Databases and MCP Hosting](modules/application-hosting.md) | Separate application databases, form data and MCP publishing |
| [Object Storage](modules/storage.md) | Local and S3-compatible storage, artifact store, file pipelines |

**Platform administration**

| Document | Description |
|------|------|
| [Authentication & Permissions](modules/auth.md) | AUTH_MODE, SSO, permission bits, admin credentials |
| [Admin Consoles](modules/admin-console.md) | The /admin and /config consoles |

## API Reference

| Document | Description |
|------|------|
| [API Overview](api/overview.md) | Response envelope, authentication, SSE protocol, full route inventory |
| [Error Codes](api/error-codes.md) | Implemented error codes, license 402, frontend handling conventions |

## Editions (Community / Enterprise)

| Document | Description |
|------|------|
| [Community vs Enterprise](editions/overview.md) | Open-core model, feature matrix, runtime forms, upgrade paths |
| [License Mechanism](editions/license.md) | Offline signature verification, state machine, feature enforcement (EE) |
| [CE Build Pipeline](editions/build-ce.md) | Manifest, build_ce.py pipeline, overlay, acceptance gates |

## Development

| Document | Description |
|------|------|
| [Backend Development](development/backend.md) | Dev model, tests, migrations, layering conventions, adding routes / MCP / skills |
| [Frontend Development](development/frontend.md) | Builds, directory conventions, API call conventions, edition gating |
| [Chat UI Customization](development/frontend-customization.md) | Page configuration, customer component packages and standalone frontends |

**Workbench applications**

| Document | Description |
|------|------|
| [Workbench Protocol](development/workbench-protocol.md) | JSON Schema, protocol versions and integration |
| [Workbench SDK and Plugin Pages](development/workbench-sdk.md) | SDK 1.0.0 and UI v3 plugin pages |
| [Development Preview](development/workbench-preview.md) | Standalone project and permission preview |
| [Build and Offline Artifacts](development/workbench-build.md) | Building and delivering the protocol, SDK and host frontend |
| [Routing and Plugin Navigation](development/workbench-routing.md) | `/apps/` URLs and applicationKey |
| [Application Resources](development/workbench-resources.md) | In-package resource runtime |
| [UI State and Bounded Keep-alive](development/workbench-state.md) | State schema versions, save and restore |
| [Badges, Notifications and Background Work](development/workbench-activity.md) | badge, notification and background capabilities |
| [Platform Adapters](development/workbench-platform-adapters.md) | Web, Tauri and Electron adapter interfaces |
| [Origins and Offline Installation](development/workbench-origins.md) | Entries for same-name local and cloud plugins |
| [Plugin Upgrades and Permission Approval](development/workbench-plugin-upgrades.md) | Upgrades, reinstalls and inherited grants |
| [Catalog and Layout Policies](development/workbench-access-policies.md) | Catalog API, availability and default entries |
| [Module Navigation and App Catalog](development/workbench-navigation-preferences.md) | Sidebar modules and the app center |
| [Catalog Notifications and Revocation](development/workbench-invalidation.md) | ETags and catalog invalidation |
| [External Applications](development/workbench-external-apps.md) | Managing and using external link applications |
| [External Application Authentication](development/workbench-external-auth.md) | Authentication setup for external applications (EE console) |
| [External Page Embedding](development/workbench-external-embedding.md) | Origins and readiness for embedded pages |
| [Application Diagnostics](development/workbench-diagnostics.md) | Diagnostic IDs and retries |
| [Error Codes](development/workbench-errors.md) | Workbench error codes |
| [Administrator Guide](development/workbench-administration.md) | Workbench administration |
| [Compatibility and Migration](development/workbench-migration.md) | Version compatibility and migration |
