# Introduction

> Last updated: 2026-07-02

HugAgentOS is an enterprise-grade AI Agent platform. Built around a ReAct agent core, it brings conversation, tool calling (MCP), skills, sandboxed code execution, long-term memory, knowledge-base RAG, automation, and batch processing together in a fully containerized (Docker Compose) system. The backend runs on FastAPI + AgentScope 2.0, the frontend on React 19 + Vite + Zustand, and every capability is registered in a unified catalog that can be toggled at will. Out of the box it is a complete, self-hostable product — not a half-finished framework.

The platform ships in two editions: the **Community Edition (CE, open source)** and the **Enterprise Edition (EE, commercial)**. CE targets individuals and small teams and includes the full conversation, tool, skill, sandbox, three-layer memory, automation, batch, and data-canvas stack. EE layers organization-scale capabilities on top: team collaboration, SSO, audit & compliance, industry data tools, persistent sandboxes, cloud storage, and the full admin console. See [Edition Comparison](../editions/overview.md) for the exact boundary.

## Feature overview

| Capability | Description | Edition |
|---|---|---|
| Conversational agent | SSE streaming chat, AgentScope 2.0 ReActAgent, Plan Mode sub-agent, deep thinking, `[ref:tool-N]` citation tracing, automatic conversation-title summarization | CE |
| Sub-agents | User-created sub-agents, @mention collaboration, routing strategy (`ROUTER_STRATEGY`) | CE (agent versioning / org-level agent library: EE) |
| MCP tool ecosystem | 10 built-in MCP servers (8 CE general tools: internet search, web fetch, chart generation, report export, batch planning, automation task management, skill management, knowledge retrieval; 2 EE industry tools: data-warehouse query and industry-chain info), served from a dedicated `mcp` container over streamable-http (`http://mcp:9100-9108/mcp/`, `http://mcp:9112/mcp/`); users can self-register remote HTTP/SSE MCP servers | CE (industry tools such as industry-chain analysis, company profiling, data-warehouse query: EE) |
| Skill system | Agent Skills (SKILL.md + scripts): built-in skill bundles, admin upload, skill marketplace browse & install, skill distillation | CE (skill review / org governance: EE) |
| Sandbox execution | `bash` / `sandbox_put_artifact` / `sandbox_get_artifact` tools with three switchable providers: script_runner (lightweight, built-in), OpenSandbox (persistent sessions + Jupyter context + snapshots), CubeSandbox (E2B-compatible MicroVM) | CE for the lightweight sandbox; persistent sandboxes (session keep-alive / snapshots): EE |
| Memory system | mem0 three-layer memory: L1 user profile, L2 vector memory (Milvus), L3 knowledge graph (Neo4j), with cross-session injection and background extraction | CE (memory audit: EE) |
| Knowledge-base RAG | Document upload, chunking, hybrid vector + keyword retrieval, private knowledge bases; optional Dify external KB integration | CE (Dify integration, enterprise-scale indexing: EE) |
| Automation | Scheduled tasks / cron scheduling / prompt & plan automation / failure retry (`orchestration/schedulers/`) | CE |
| Batch execution | Excel / Word / list templates with placeholder substitution, batch plan generation and execution (batch_runner MCP) | CE (team quota billing: EE) |
| Data canvas | Online spreadsheet editing built on Univer | CE for personal editing; real-time multi-user collaboration: EE |
| Projects / My Space | Personal projects (file capacity quota), personal drive (myspace), favorites, conversation sharing | CE (team folders / permission matrix: EE) |
| Admin consoles | `/admin` content management (skills, capability center, release notes) + `/config` system console (prompt version pool, models, users/teams, billing, audit, security) | CE basics; full console (users/teams/billing/audit/security): EE |

## CE vs. EE

In one sentence: **the Community Edition lets one person push the platform to its limits; the Enterprise Edition lets an organization run it at scale** — team collaboration, SSO, RBAC, audit & compliance, industry data tools, persistent sandboxes, cloud storage, and white-labeling belong to EE. See [Edition Comparison](../editions/overview.md) and the [License mechanism](../editions/license.md) for the full matrix.

## Technology stack

| Layer | Technology | Notes |
|---|---|---|
| Backend | FastAPI + Uvicorn (Python 3.11) | `src/backend/api/app.py`, unified response envelope |
| Agent framework | AgentScope 2.0 (`agentscope==2.0.0`) | ReActAgent + tool registration, `core/llm/agent_factory.py` |
| Frontend | React 19 + Vite 7 + Zustand 5 + Ant Design 6 | `src/frontend/`, served by nginx with an `/api` reverse proxy |
| Database | PostgreSQL 15 (production) / SQLite (local-debug fallback) | SQLAlchemy 2 + Alembic migrations |
| Cache / sessions | Redis 7 | Session store, streaming follower (Redis Streams) |
| Vector DB | Milvus 2.4 (`mem0` profile) | L2 vector memory and self-hosted KB retrieval |
| Graph DB | Neo4j 5 Community (`mem0` profile, optional) | L3 knowledge-graph memory |
| Sandbox | script_runner sidecar / Alibaba OpenSandbox / Tencent CubeSandbox | `core/sandbox/` provider protocol, switched via env |
| Deployment | Docker Compose (profiles: `script_runner` / `opensandbox` / `mem0`) | Everything runs in containers; there is no local dev server |

## Architecture at a glance

```
                        ┌──────────────────────────────────────────────┐
 Browser ──► Nginx ────►│  FastAPI backend (src/backend/api/app.py)    │
        (frontend ctr,  │   api/routes/v1/* · 50+ routers · envelope   │
         /api proxy)    └───────────────────┬──────────────────────────┘
                                            │
                     ┌──────────────────────┼─────────────────────────┐
                     ▼                      ▼                         ▼
        orchestration/workflow.py     core/services/*          core/auth/*
        (SSE streaming orchestration: (business service       (local / mock /
         text / tool_call /            layer)                  remote + SSO)
         tool_result / meta / done)
                     │
       ┌─────────────┼───────────────────┬─────────────────────┐
       ▼             ▼                   ▼                     ▼
 core/llm/      orchestration/     orchestration/        core/memory/ (svc)
 agent_factory  strategy.py        citations.py          + memory_integration
 (AgentScope    (routing           (citation parsing)    (mem0 retrieve/save)
  2.0 ReAct)     strategy)                                      │
       │                                                        ▼
       ├──► core/llm/mcp_manager ──► mcp container          Milvus / Neo4j
       │      (10 MCP servers, http://mcp:9100-9108/mcp/ + :9112) (mem0 profile)
       ├──► core/sandbox/* ──► script-runner / OpenSandbox / CubeSandbox
       └──► PostgreSQL · Redis · storage (local / S3 / OSS)
```

Main request path: browser → nginx inside the frontend container (`/api` proxy) → FastAPI → `orchestration/workflow.py` orchestrates streaming → `core/llm/agent_factory.py` builds the ReActAgent → MCP tools / sandbox / memory → SSE events (`text` / `tool_call` / `tool_result` / `meta` / `done`) stream back to the frontend.

## Next steps

- [Quick Start in 10 minutes](quick-start.md)
- [Full Docker Compose deployment](../deployment/docker-compose.md)
- [Environment variable reference](../deployment/environment-variables.md)
- [Architecture overview](../architecture/overview.md)

## Related source

| Feature | Files |
|---|---|
| FastAPI app & router registration | `src/backend/api/app.py`, `src/backend/api/routes/v1/` |
| Streaming orchestration (SSE) | `src/backend/orchestration/workflow.py`, `orchestration/streaming.py` |
| Agent construction | `src/backend/core/llm/agent_factory.py` |
| MCP servers & port mapping | `src/backend/mcp_servers/`, `src/backend/mcp_servers/_ports.py`, `src/backend/core/config/mcp_config.py` |
| Capability catalog | `src/backend/core/config/catalog.json`, `core/config/catalog.py` |
| Sandbox providers | `src/backend/core/sandbox/`, `src/backend/core/config/settings.py::SandboxSettings` |
| Memory system | `src/backend/core/memory/` (service.py / pipeline.py), `src/backend/orchestration/memory_integration.py` |
| Automation scheduler | `src/backend/orchestration/schedulers/automation_scheduler.py` |
| Frontend entries (App / Admin / Config) | `src/frontend/src/main.tsx`, `App.tsx`, `AdminApp.tsx`, `ConfigApp.tsx` |
| Edition & license settings | `src/backend/core/config/settings.py::EditionSettings / LicenseSettings` |
