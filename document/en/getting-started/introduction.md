# Introduction

> Last updated: 2026-10-23 | [简体中文](../../zh-CN/getting-started/introduction.md)

HugAgentOS is an open-source, self-hosted **agent harness**. The model does the thinking; the
harness handles everything around it: where a task runs, how it picks up after an interruption,
how tools and sandboxes are used, which actions need human approval, and how experience is kept.

At its core is the **durable agent**: your request runs in the cloud, not in your browser or on
your computer. Close the tab or shut the laptop and the task keeps moving; if a server restarts or
fails, another instance continues from the last saved progress; external operations that may or
may not have happened are never redone automatically. On top of that, HugAgentOS provides an
always-on **Personal Agent** and runs in two forms: **HugAgentOS Cloud** and **HugAgentOS Local**.

The platform ships as a **Community Edition (CE, open source)** and an **Enterprise Edition (EE)**.
CE lets an individual run the complete harness and Personal Agent; EE adds organization-scale
capabilities such as team collaboration, SSO, compliance auditing and industry data tools. See the
[edition comparison](../editions/overview.md) for the exact boundary.

## Runtime forms

| Form | Runs on | Data and state | Best for | Guide |
|---|---|---|---|---|
| **HugAgentOS Cloud** | Your server or private cloud | PostgreSQL, Redis, S3-compatible storage, OpenSandbox | 24/7 Personal Agent, long tasks, IM channels, access from any device | [Docker Compose](../deployment/docker-compose.md) |
| **HugAgentOS Local** | Your computer (Windows / macOS / Linux) | Local SQLite, Milvus Lite, local files | Offline work, sensitive data, working directly on local projects | [Desktop local mode](../deployment/desktop-local-mode.md) |
| **Local + cloud** | Cloud as control plane, local machine as execution plane | Cloud projects in the cloud, local projects on the machine | An always-on cloud agent that also operates your computer and projects | [Desktop local mode](../deployment/desktop-local-mode.md) |
| One-command install | A development machine | SQLite, in-process state, subprocess sandbox | Trials and development | [No-Docker quick install](../deployment/quick-install.md) |

Durable 24/7 operation needs HugAgentOS Cloud. The one-command install has no cross-process lease
takeover and is meant for trials only.

## Feature overview

| Capability | Description | Edition |
|---|---|---|
| Durable runs | Every request is a persisted run executed in the background; resumable streams and mid-run steering; lease takeover and startup reconciliation; tool side effects record intent and receipt, and unknown outcomes are never replayed automatically | CE |
| Personal Agent | One conversation, parallel background tasks, a permanent cloud computer, schedules and heartbeats, IM channels, action approvals, file-based memory with Dreaming, and connecting your own computer | CE (authorization governance and cross-device session management are EE) |
| Autonomous loop | Goals survive across turns until done; a worker does the work, a read-only reviewer verifies it, and command-checkable requirements are verified by the driver; resumable after interruption | CE |
| Chat and Plan Mode | SSE streaming, ReAct tool orchestration, deep thinking, Plan Mode, citations, context compaction checkpoints, chat forks | CE |
| Sub-agents | Built-in explorer, worker and reviewer, user-created sub-agents, automatic routing or `@` mentions | CE (organization agent library is EE) |
| MCP tools and plugins | Built-in web search, page fetching, knowledge retrieval, charts, automation, and skill and plugin management tools; an MCP marketplace, private MCP servers, a plugin marketplace and a browser automation plugin | CE (data warehouse and industry tools are EE) |
| Agent Skills | SKILL.md plus scripts; built-in skills, a skill marketplace and personal skills | CE (skill review and organization governance are EE) |
| Sandbox execution | OpenSandbox sessions with environment reuse and snapshot recovery, or a local subprocess sandbox; workflow jobs run as managed processes that can be taken over by lease | CE (CubeSandbox and team mounts are EE) |
| Memory and personal evolution | L1 profile, L2 procedural memory (Milvus, optional), L3 relationship graph (Neo4j, optional); durable outbox writes; evolution proposals approved one by one | CE (memory auditing is EE) |
| Knowledge base | Document chunking, hybrid vector and keyword retrieval, private knowledge bases, knowledge-base wiki | CE (public KB administration and Dify integration are EE) |
| Domain ontology governance | Domain Pack import and versioned publishing, rule checks before tool execution, independent review of high-risk answers, human review of evolution drafts | CE |
| Automation | Scheduled tasks in natural language, cron scheduling, retries on failure | CE |
| Sites and application hosting | Build a site in chat and publish it in one click; separate application databases and MCP hosting | CE (site collaboration after project conversion is EE) |
| Data canvas and artifacts | Univer spreadsheets, charts, reports, Office files, web pages and an artifact center | CE personal editing; real-time collaboration is EE |
| Projects and My Space | Personal projects, folders, favourites and chat sharing; the desktop client can bind local project directories | CE (team folders and permission matrices are EE) |
| Desktop client | Tauri 2 client with its own private runtime, supporting local-only, cloud-only and local + cloud modes | CE |

## Technology stack

| Layer | Technology |
|---|---|
| Agent runtime | AgentScope 2.0 (ReActAgent), Model Context Protocol |
| Backend | Python 3.11, FastAPI, SQLAlchemy 2, Alembic |
| Frontend | React 19, TypeScript, Vite, Zustand, Ant Design |
| Desktop | Tauri 2, private CPython 3.11 runtime |
| Data and state | PostgreSQL 15 or SQLite; Redis 7 or in-process state |
| Storage | S3-compatible storage (cloud), local files (desktop and one-command install) |
| Sandbox | OpenSandbox, local subprocess (CubeSandbox is EE) |
| Optional memory | Milvus 2.4, Neo4j 5 Community, mem0 |
| Deployment | Docker Compose, desktop installer, one-command installer, Nginx |

## Architecture

```
 Entry points: web · desktop client · IM channels · API · schedules · heartbeats
                         │
                         ▼
 FastAPI (api/)  auth · edition routing · messages persisted before the response
                         │
                         ▼
 Run layer (core/runs · core/chat/runtime)
   persisted runs · database leases · startup reconciliation · resumable streams · steering · tool effect receipts
                         │
                         ▼
 Agent execution (core/agent_runtime · core/llm)
   ReActAgent · per-step context assembly and compaction · memory / ontology / evolution integration
   Outer orchestration (orchestration/): Plan Mode · autonomous loop · automation
                         │
        ┌────────────────┼──────────────────┐
        ▼                ▼                  ▼
  MCP · skills · plugins  sandbox · jobs   knowledge · memory
        │                │                  │
        └────────────────┴──────────────────┘
                         ▼
 PostgreSQL / SQLite · Redis · S3 / local storage · Milvus · Neo4j
```

Personal Agent (`personal_agent/`) reuses the same runs, leases and sandboxes, and adds identity,
a task queue, scheduling, channels, device links and workspace files. See the
[architecture overview](../architecture/overview.md) for the full request lifecycle.

## Next steps

- [Quick start](quick-start.md)
- [Personal Agent](../modules/personal-agent.md)
- [Chat and orchestration](../modules/chat.md)
- [Autonomous loop](../modules/autonomous-loop.md)
- [Deployment guide](../deployment/README.md)

## Source map

| Area | Path |
|---|---|
| FastAPI app and routes | `src/backend/api/app.py`, `src/backend/api/routes/v1/` |
| Run lifecycle, leases and recovery | `src/backend/core/runs/` |
| Chat worker, stream resume and steering | `src/backend/core/chat/runtime/` |
| Agent execution entry | `src/backend/core/agent_runtime/` (`runner.astream_chat_workflow`) |
| Agent construction | `src/backend/core/llm/factory/build.py` |
| Harness contracts and execution ledger | `src/backend/core/harness/` |
| Workflow background jobs | `src/backend/core/jobs/` |
| Plan Mode, autonomous loop, automation | `src/backend/orchestration/plans/`, `orchestration/loops/`, `orchestration/automations/` |
| Personal Agent | `src/backend/personal_agent/` |
| Memory and evolution | `src/backend/core/memory/`, `src/backend/core/evolution/` |
| Domain ontology | `src/backend/core/ontology/` |
| Sandbox | `src/backend/core/sandbox/` |
| Built-in MCP, skills, plugins and agents | `src/backend/extensions/` |
| Frontend entry | `src/frontend/src/main.tsx`, `App.tsx` |
| Desktop client | `desktop/src-tauri/` |
