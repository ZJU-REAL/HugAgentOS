# Community vs. Enterprise Edition
> Last updated: 2026-07-22

HugAgentOS is distributed under an **open-core** model, following the established practice of open-source agent platforms such as Dify and FastGPT:

- **Community Edition (CE)** — open source and free. A single person on a single machine gets a fully self-contained platform: complete chat, knowledge-base RAG, sub-agents, general-purpose tools, automation, batch execution, data canvas, and three-layer personal memory.
- **Enterprise Edition (EE)** — adds the **organization-scale** capabilities on top of CE: team collaboration, SSO, audit & compliance, billing, industry data tools, admin consoles, and white-labeling. Licensed by annual subscription via an offline license file.

The dividing line in one sentence: **capabilities that are self-contained for an individual go to CE; capabilities that only matter at organizational scale go to EE**.

## Repository model

This repository (the main repo) is the single development source of truth for the Enterprise Edition; it defaults to `JX_EDITION=ee` (see `.env.example`). The Community Edition is **not a separately maintained branch** — it is a subset tree **deterministically derived** from the main repo by the build pipeline:

```bash
python scripts/build_ce.py        # generates dist/ce/ (the CE derived tree)
```

EE-only code is **physically absent** from the derived tree (the whitelist iron rule). CE and EE share the `HugAgentOS` display brand and retain `hugagent` as the technical identifier; see [CE Build Pipeline](build-ce.md) for derivation details.

```
Main repo (EE, this repository)
  ├── Commercial delivery: image bundle + license file (offline activation)
  └── scripts/build_ce.py ──► dist/ce/ (CE derived tree, published as open source)
```

## Feature comparison matrix

The edition boundary is aligned with the public pricing page. The table below reflects what is actually implemented in code today:

| Capability | Community (CE) | Enterprise (EE) adds |
|---|---|---|
| Chat | ✅ Full: SSE streaming, ReAct agent, plan mode, deep thinking, cited sources | — |
| Knowledge-base RAG | ✅ Document upload, smart chunking, hybrid vector + keyword retrieval, private KBs | ➕ External KB integration (Dify), public KB admin console |
| Sub-agents | ✅ Create, auto-routing, @-mention collaboration (personal) | ➕ Organization-level agent library & admin console |
| MCP tools | ✅ 8 general tools: internet search, web fetch, chart generation, report export, batch runner, automation task management, skill management, KB retrieval | ➕ 2 industry tools: data-warehouse query (`query_database`), industry-chain knowledge center (`ai_chain_information_mcp`) |
| Self-service capability hub | ✅ Bring your own private MCP servers (remote HTTP/SSE) and private skills (hand-written or zip upload), owner-isolated | ➕ Org-level permission governance, skill review & distillation |
| Personal API keys | ✅ Create / revoke your own API keys for the native agent API | ➕ External model gateway (OpenAI / Anthropic compatible) plus org-managed per-user authorization bits |
| Memory | ✅ L1 personal profile + L2 vector (Milvus, optional) + L3 graph (Neo4j, optional) | ➕ Memory audit (compliance trail, `memory_audit` table) |
| Automation | ✅ Scheduled tasks, cron, prompt/plan automation, retry on failure | — |
| Batch execution | ✅ Excel/Word/list template batch processing | — |
| Data canvas | ✅ Online spreadsheet personal editing (free Univer presets) | ➕ Real-time multi-user collaboration (with the commercial `@univerjs/preset-sheets-advanced`) |
| Code execution | ✅ Lightweight sandbox (script-runner) + offload/read-back of oversized results | ➕ Persistent sandbox (OpenSandbox / Cube providers; session retention, environment reuse) |
| File storage | ✅ Local storage | ➕ Cloud storage (S3 / Alibaba Cloud OSS) |
| Authentication | ✅ Local account sign-up & login | ➕ Enterprise SSO, department sync, invite codes |
| Personal workspace | ✅ Personal folders, favorites, chat sharing, personal projects | — |
| Team collaboration | — | ➕ Teams, member management, team folders, permission matrix, team chat sharing |
| Security & audit | — | ➕ Operation audit, chat history review, invocation logs, security console |
| Billing & usage | ✅ View your own token usage | ➕ Billing reports (aggregation, model pricing, CSV export); quota enforcement (planned) |
| Content admin console (/admin) | — (replaced by the personal capability hub) | ➕ Skills / prompt version canary / MCP / agents / KB management |
| System console (/config) | — | ➕ Users / teams / invites / security / service configs / License panel |
| Branding | ⚠️ Rebrandable (attribution retained) | ➕ Full white-label |

> Note the 2026-06 boundary adjustment: **automation, batch execution, data canvas (personal editing), and memory L2 vector / L3 graph moved from EE down to CE**. EE keeps only their organizational increments (canvas multi-user collaboration, team quota billing, etc.).

## How edition and license manifest at runtime

Two environment variables define the deployment shape (`EditionSettings` / `LicenseSettings` in `src/backend/core/config/settings.py`):

| Shape | Configuration | Behavior |
|---|---|---|
| Community | `JX_EDITION=ce` (default in the CE tree's `.env.example`) | The CE tree physically contains no license implementation; its edition probe reports the CE shape and an empty EE capability set, with no signature verification or seat limit |
| Enterprise · internal | `JX_EDITION=ee`, no license file configured, and `JX_LICENSE_REQUIRED=false` (main-repo default) | **Internal / fully-managed mode: everything enabled** — identical to historical behavior, so existing deployments need zero config changes after upgrading to a license-aware release |
| Enterprise · licensed | `JX_EDITION=ee` + a valid license file | Gated by the license entitlement (feature list + seats + validity window) |

Every deployment exposes the unauthenticated probe `GET /v1/meta/edition` (`src/backend/api/routes/v1/meta.py`) returning `edition` / `mode` / a boolean feature map; the frontend `stores/editionStore.ts` fetches it at startup and hides EE entry points (e.g. the Teams tab) accordingly. The full state machine and enforcement model are described in [License Mechanism](license.md).

## How to obtain CE

The Community Edition is published as a **derived tree**:

1. Upstream runs `scripts/build_ce.py` against a tagged release of the main repo, producing `dist/ce/`;
2. Generation must pass the brand gate (zero hits) plus import / pytest / frontend-build self-checks;
3. `dist/ce/` is published as a standalone open-source repository (`ce/overlay/README.md` is its README; it is marked `generated`, and `src/**` changes are fed back via Issues / Discussions).

Quick start in the CE tree:

```bash
cp .env.example .env
docker compose up -d --build  # frontend :3002 · backend :3001
# After startup, log in and configure model access under Settings → System → Model Services
# (search engine keys etc. under Service Config)
# Optional L2/L3 memory components:
COMPOSE_PROFILES=mem0 docker compose up -d
```

## Upgrade paths

- **CE → EE**: the Enterprise Edition is delivered as an **image bundle + license file** (suitable for air-gapped government networks). The CE table set is a strict subset of EE's: 20 EE tables, their foreign keys, and commercial-scope columns on shared resources are not registered; delivery migration adds organization structures during an upgrade.
  > ⚠️ Caveat: CE runs an independent migration chain (baseline `ce_0001`, see [CE Build Pipeline](build-ce.md#ce-database-differences)) which differs from the EE chain; reconciling the alembic version of an existing CE database when switching to the EE image is handled during delivery — there is no automated conversion tool yet (planned).
- **EE internal → EE licensed**: for private delivery, set `LICENSE_KEY_PATH` and `JX_LICENSE_REQUIRED=true`, then upload the `.lic` file in the License panel of the `/config` console — activation is immediate, no restart needed.
- **Renewal / expansion**: upload a new license file to hot-swap (same flow); after expiry there is a grace window (14 days by default).

## Related source

| Topic | Path |
|---|---|
| Edition / license settings | `src/backend/core/config/settings.py` (`EditionSettings` / `LicenseSettings`) |
| License state machine | `src/backend/edition_ee/licensing/manager.py` |
| EE feature enum | `src/backend/edition_ee/licensing/features.py` |
| Router registry (CE/EE tables) | `src/backend/api/routes/v1/__init__.py` |
| Edition probe | `src/backend/api/routes/v1/meta.py` |
| Frontend edition gating | `src/frontend/src/stores/editionStore.ts` |
| CE derivation manifest | `ce/manifest.yaml` |
| CE generator | `scripts/build_ce.py` |

Next: [License Mechanism (EE)](license.md) · [CE Build Pipeline](build-ce.md) · [Backend Development Guide](../development/backend.md)
