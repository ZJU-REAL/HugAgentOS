<p align="center">
  <img src="./src/frontend/public/home/hugagentos-logo.png" alt="HugAgentOS logo" width="800" />
</p>

<p align="center">
  <strong>HugAgent: The Enterprise AgentOS for Ontology-Grounded Trustworthy Reasoning</strong>
</p>

<p align="center">
  The open-source, self-hosted foundation for enterprise AI agents
</p>

<p align="center">
  Give models the context and tools to retrieve knowledge, work with files,
  run code, and carry real tasks through to completion.
</p>

<p align="center">
  <a href="./README.md">English</a> ·
  <a href="./README_CN.md">简体中文</a>
</p>

<!-- Keep these stable launch URLs so they can go live without another README redesign. -->
<p align="center">
  <a href="https://hugagentos.com">Website</a> ·
  <a href="https://app.hugagentos.com">Try HugAgentOS online</a>
</p>

<p align="center">
  <a href="./LICENSE">
    <img src="https://img.shields.io/badge/License-Apache_2.0_%2B_terms-2E8B57?style=flat-square" alt="Apache 2.0 with supplementary terms" />
  </a>
  <a href="./document/en/editions/overview.md">
    <img src="https://img.shields.io/badge/Edition-Community-635BFF?style=flat-square" alt="Community Edition" />
  </a>
  <a href="./document/en/deployment/quick-install.md">
    <img src="https://img.shields.io/badge/Install-One_command-0F766E?style=flat-square" alt="One-command installation" />
  </a>
  <a href="./document/en/architecture/overview.md">
    <img src="https://img.shields.io/badge/Agent-AgentScope_2.0-FF6A00?style=flat-square" alt="AgentScope 2.0" />
  </a>
  <a href="./document/en/modules/mcp-tools.md">
    <img src="https://img.shields.io/badge/Tools-MCP-111827?style=flat-square" alt="Model Context Protocol" />
  </a>
</p>

HugAgentOS is the open-source foundation of HugAgent, an enterprise-grade
AgentOS that treats domain ontology as a control plane for agent reasoning,
decisions, and actions. It combines agentic chat, private knowledge-base RAG,
sub-agents, MCP tools, Agent Skills, sandboxed execution, long-term memory,
automation, and a data canvas in one self-hosted workspace.

> [!NOTE]
> This Community repository is generated from the upstream main repository for
> each release and is marked `generated`. Report changes to `src/**` through an
> Issue or Discussion. Pull requests for documentation and examples are
> welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) for details.

## Quick start

Install the personal, single-machine profile on Linux, macOS, or WSL2 with one
command. You need Python 3.11 or later, Node.js 20 or later, Git, and access to
an LLM API. On Linux platforms without a compatible prebuilt `ripgrep` wheel,
you also need the current stable Rust toolchain. You don't need Docker,
PostgreSQL, or Redis.

```bash
curl -fsSL https://raw.githubusercontent.com/ZJU-REAL/HugAgentOS/main/install.sh | bash
```

The installer clones HugAgentOS into `~/.hugagent/source`, creates an isolated
Python environment, installs the dependencies, builds the web application, and
opens the first-run wizard. Follow the prompts to create an administrator and
connect an OpenAI-compatible or local model. HugAgentOS then opens at
[http://127.0.0.1:3001](http://127.0.0.1:3001).

A fresh data directory creates exactly one local administrator. The initial
username and password are both `admin`, and the password must be changed on the
first sign-in. CE does not provide self-service registration.

> [!WARNING]
> The one-click server listens on `127.0.0.1` by default. If you need remote
> access to a server, run
> `hugagent serve --host 0.0.0.0 --port 3001 --no-browser`, and configure a
> strong administrator password, a firewall, and HTTPS first. Don't expose the
> service directly on an untrusted network.

Start HugAgentOS again at any time with this command:

```bash
~/.hugagent/venv/bin/hugagent
```

> [!NOTE]
> The one-command profile is designed for personal trials and development. It
> uses SQLite, in-process state, and a local subprocess sandbox. For teams or
> production, use the [Docker Compose deployment
> guide](./document/en/deployment/docker-compose.md).

For installer options, capability boundaries, and troubleshooting, read the
[no-Docker installation guide](./document/en/deployment/quick-install.md).

## From answers to outcomes

HugAgentOS isn't another wrapper around a chat box. It puts the context,
execution environment, and artifact management an agent needs into one task
flow.

<table>
  <tr>
    <td width="50%" valign="top">
      <strong>🔌 Bring your model</strong><br />
      Connect cloud or local models through one provider layer without locking
      the application to a single vendor.
    </td>
    <td width="50%" valign="top">
      <strong>🛠️ Take action</strong><br />
      ReAct orchestration combines MCP, skills, and a sandbox to search,
      analyze, create files, and call external capabilities.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🧠 Retain context</strong><br />
      Private knowledge bases and layered memory provide context across files
      and conversations.
    </td>
    <td width="50%" valign="top">
      <strong>🏠 Own your data</strong><br />
      Run the application, databases, and file storage on infrastructure you
      control.
    </td>
  </tr>
</table>

## Enterprise trust through domain ontology

HugAgent uses domain ontology as an executable control plane, not only as a
knowledge store. Controlled concepts, relationships, invariants, action
contracts, roles, and permissions give the skill, memory, and orchestration
engines one shared business language.

<table>
  <tr>
    <td width="50%" valign="top">
      <strong>🧭 Shared semantic ground</strong><br />
      Align domain intent, skills, tools, memory, and agent roles against one
      versioned set of concepts and relationships.
    </td>
    <td width="50%" valign="top">
      <strong>🏗️ Build-time governance</strong><br />
      Validate skills, tools, and sub-agents as they are created or imported,
      and assemble capabilities through consistent Action contracts.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🛡️ Policy-gated execution</strong><br />
      Turn a candidate plan into action through deterministic rule checks,
      risk-based evidence review, and gated execution. Violations return with
      the rule, evidence, and repair guidance instead of silently proceeding.
    </td>
    <td width="50%" valign="top">
      <strong>🔎 Traceable, governed evolution</strong><br />
      Record approvals, rejections, evidence, and outcomes. Enforcement events
      become versioned ontology proposals that require human review and remain
      reversible.
    </td>
  </tr>
</table>

> [!NOTE]
> The ontology trust plane is a target enterprise architecture being integrated
> incrementally on top of the current Harness. It strengthens structured
> compliance and evidence-backed review; it doesn't claim to eliminate every
> free-text hallucination.

## Core capabilities

Community Edition covers the complete personal-agent loop from conversation
and execution to retention and reuse. Optional infrastructure stays optional
during the first run.

<table>
  <tr>
    <td width="50%" valign="top">
      <strong>💬 Agentic chat and Plan Mode</strong><br />
      SSE streaming, ReAct tool orchestration, deep thinking, Plan Mode,
      traceable citations, and resumable streams.
    </td>
    <td width="50%" valign="top">
      <strong>📚 Private knowledge-base RAG</strong><br />
      Document ingestion and chunking, hybrid vector and keyword retrieval,
      optional reranking, and private knowledge isolation.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🤝 Personal sub-agents</strong><br />
      Create agents with focused roles, then collaborate through automatic
      routing or <code>@</code> mentions.
    </td>
    <td width="50%" valign="top">
      <strong>🔧 MCP tool ecosystem</strong><br />
      Built-in web search, page fetching, knowledge retrieval, charts, reports,
      batch jobs, automation, and skill management.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🧩 Agent Skills</strong><br />
      Extend agents with structured instructions and scripts through bundled
      skills, a skill marketplace, and personal skills.
    </td>
    <td width="50%" valign="top">
      <strong>⚙️ Automation and batch execution</strong><br />
      Create scheduled tasks in natural language or apply one workflow across
      spreadsheets, Word documents, and file lists.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🧪 Sandbox and artifacts</strong><br />
      Run code in a local subprocess or lightweight container sandbox, then
      create charts, reports, Office files, websites, and data-canvas artifacts.
    </td>
    <td width="50%" valign="top">
      <strong>🧠 Three-tier personal memory</strong><br />
      Store the L1 personal profile in the relational database, with optional
      Milvus vector memory and Neo4j graph memory.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🗂️ Personal workspace</strong><br />
      Organize long-running work with projects, folders, favorites, shared
      conversations, and an artifact center.
    </td>
    <td width="50%" valign="top">
      <strong>📊 Data canvas</strong><br />
      Inspect and edit structured data inside the conversation so the analysis
      and final result stay in one workspace.
    </td>
  </tr>
</table>

## Architecture

HugAgentOS combines a three-engine Agent Harness with an ontology trust plane.
The ontology aligns capabilities at build time, injects relevant domain rules
at runtime, and gates actions before execution. Every decision can feed a
traceable, human-governed improvement loop.

```mermaid
flowchart TB
    U[Enterprise users and channels] --> API[Web / Desktop / API]
    API --> RUN[ChatRun and streaming workflow]

    ONTO[Domain ontology control plane<br/>Concepts · Relationships · Invariants<br/>Action contracts · Roles · Permissions]
    ONTO --> BUILD[Build-time validation<br/>Skills · Tools · Sub-agents]
    ONTO --> BOOT[Domain bootstrap<br/>Semantic alignment · Relevant rules]

    subgraph HARNESS[Three-engine Agent Harness]
        direction LR
        SKILL[Skill engine] <--> ORCH[Orchestration engine]
        ORCH <--> MEMORY[Memory engine]
    end

    RUN --> ORCH
    BUILD --> SKILL
    BOOT --> ORCH
    BOOT --> MEMORY
    ORCH --> PLAN[Candidate plan or action]

    PLAN --> RULE[Deterministic ontology rule gate]
    ONTO --> RULE
    RULE -->|Low risk and compliant| EXEC[Gated execution]
    RULE -->|Checkpoint or high risk| REVIEW[Evidence-backed review]
    REVIEW -->|Approved| EXEC
    RULE -->|Violation| REVISE[Reject with evidence and repair guidance]
    REVIEW -->|Revise or escalate| REVISE
    REVISE --> ORCH

    EXEC --> CAP[MCP · Skills · Sandbox · RAG]
    EXEC --> AUDIT[Traceable audit and replay]
    REVISE --> AUDIT
    AUDIT --> EVOLVE[Governed ontology proposals]
    EVOLVE -. Human review · Versioning · Rollback .-> ONTO

    API --> DATA[(SQLite / PostgreSQL · Redis · Object storage)]
    MEMORY --> VECTOR[(Milvus / Neo4j, optional)]
```

### Technology stack

The project combines mature, replaceable open-source components behind clear
service boundaries.

| Layer | Main technologies |
|---|---|
| Agent runtime | AgentScope 2.0, ReAct, Model Context Protocol |
| Backend | Python, FastAPI, SQLAlchemy, Alembic |
| Frontend | React 19, TypeScript, Vite, Zustand, Ant Design |
| Data and state | SQLite or PostgreSQL 15, in-process state or Redis 7, local file storage |
| Optional memory | Milvus 2.4, Neo4j 5 Community, mem0 |
| Deployment | One-command local installer, Docker Compose, Nginx |

See the [architecture overview](./document/en/architecture/overview.md) for the
full request lifecycle, container topology, and design decisions.

## Community and Enterprise editions

Community Edition gives an individual a complete agent workspace. Enterprise
Edition adds the governance, collaboration, and delivery capabilities needed
to operate the same experience across an organization. Enterprise-only source
is physically absent from the Community tree.

| Community Edition | Enterprise Edition adds |
|---|---|
| Agentic chat, Plan Mode, and personal sub-agents | Teams, organization agents, and permission matrices |
| 8 general MCP tools, personal skills, and a skill marketplace | Industry data tools, organization governance, and skill review |
| Private knowledge bases and three-tier personal memory | Public knowledge administration and memory auditing |
| Automation, batch execution, and a personal data canvas | Organization billing, usage reports, and canvas collaboration |
| Lightweight sandbox and local file storage | Persistent sandboxes, cloud storage, and offline delivery |
| Local accounts and branding with Powered-by attribution | SSO, compliance auditing, and full white-labeling |

See the [edition overview](./document/en/editions/overview.md) for the complete
feature boundary and upgrade path.

## Documentation

The repository includes complete English and Chinese documentation for
operators, users, and contributors, and you can read it offline.

| Goal | English | 中文文档 |
|---|---|---|
| Understand the product | [Introduction](./document/en/getting-started/introduction.md) | [产品简介](./document/zh-CN/getting-started/introduction.md) |
| Run it in 10 minutes | [Quick start](./document/en/getting-started/quick-start.md) | [快速开始](./document/zh-CN/getting-started/quick-start.md) |
| Configure a deployment | [Deployment](./document/en/deployment/README.md) | [部署指南](./document/zh-CN/deployment/README.md) |
| Explore the system design | [Architecture](./document/en/architecture/overview.md) | [架构总览](./document/zh-CN/architecture/overview.md) |
| Learn MCP, skills, memory, and sandboxing | [Modules](./document/en/README.md#modules) | [功能模块](./document/zh-CN/README.md#功能模块) |
| Build backend or frontend features | [Development](./document/en/README.md#development) | [开发指南](./document/zh-CN/README.md#开发指南) |

Start from [document/README.md](./document/README.md) to browse every guide.

## Contributing

We welcome bug reports, feature proposals, documentation improvements, and
reproducible patches. Read [CONTRIBUTING.md](./CONTRIBUTING.md) before you
start so you understand the boundary between generated and directly editable
content.

- Include reproduction steps, expected behavior, actual behavior, and your
  environment in bug reports.
- Explain the concrete use case and problem when proposing a feature.
- Keep English and Chinese documentation aligned with the Community and
  Enterprise edition boundary.

Don't open a public Issue for a security vulnerability. Follow
[SECURITY.md](./SECURITY.md) to report it through a private channel.

## License

HugAgentOS Community Edition is licensed under Apache License 2.0 with
supplementary terms. The terms restrict operating the software as a competing
multi-tenant SaaS offering and require the UI's Powered-by attribution to
remain visible. [LICENSE](./LICENSE) and [NOTICE](./NOTICE) define the complete
rights and obligations for internal use, modification, and distribution.
