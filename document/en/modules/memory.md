# Memory System (mem0)

> Last updated: 2026-06-11

HugAgentOS ships with a **layered persistent memory system** built on [mem0](https://github.com/mem0ai/mem0). When enabled, the agent remembers a user's identity, preferences, and historical facts across sessions, and automatically brings that context into new conversations. Memory is organized into three layers by information stability (L1 profile / L2 vector facts / L3 knowledge graph) — all three layers are Community Edition capabilities; only **memory auditing** (compliance trail) belongs to the commercial edition (Enterprise Edition, EE).

The whole system honors one core promise: **no memory I/O ever blocks the SSE hot path** — retrieval runs as a background task with a time budget, and writes go through a bounded post-response pipeline after the SSE stream closes (see the module docstring in `src/backend/core/memory/__init__.py`).

## Layer model

| Layer | Name | Storage | Injection point | Implementation |
|---|---|---|---|---|
| L1 | Profile | DB (bounded markdown, 1500-char default cap) | Frozen into context at session start | `core/memory/profile.py` |
| L2 | Facts | Milvus vector store (collection `hugagent_memories`) | Top-K similarity retrieval at session start | `core/memory/service.py` (mem0 wrapper) |
| L3 | Graph | Neo4j (optional, `MEM0_GRAPH_ENABLED=true`) | On-demand retrieval | `core/memory/service.py` (mem0 `enable_graph`) |
| — | Session working set | `chats.metadata.session_memory` | Within a single session | per-session task working set |
| — | Audit side channel (Enterprise Edition, EE) | DB table `memory_audit` | Side-recorded on every read/write | `core/memory/audit.py` |

## Data flow

```
User sends a message
  │
  ▼
api/routes/v1/chats.py
  · reads memory_enabled / memory_write_enabled from users_shadow.metadata
  · project chats read projects.metadata instead (team projects scope = "team:<team_id>")
  │
  ▼
orchestration/workflow.py
  ├─► launch_memory_retrieval()            ← background task, returns immediately
  │     └─ core/memory/service.retrieve_memories()
  │          └─ mem0.Memory.search() → Milvus vector search (+ optional Neo4j graph search)
  │
  ├─► build_frozen_memory_block()          ← assembles the "session-frozen" block
  │     · L1 profile: DB read, <20ms, always awaited
  │     · L2 facts: awaits the retrieval task within a 600ms budget
  │       (MEMORY_RETRIEVAL_BUDGET_MS); on timeout, injection is skipped
  │       so agent startup is never blocked
  │
  ├─► inject_frozen_memory()               ← frozen block prepended to
  │                                           session_messages as a user-role message
  │     (user rather than system: models like Qwen require system only at index 0)
  │
  ▼  … agent streams its response over SSE …
  │
  ▼  after SSE closes (the user never waits)
save_memories_background()
  └─ core/memory/pipeline.schedule_post_response_tasks()
       · global semaphore caps concurrency (default 8)
       · keyword classification → runs 0–4 extractors (identity/preference/fact/task)
       · each extractor has its own 30s timeout
       · sanitizer gate → write L1/L2/session → audit side channel
```

The integration layer for retrieval and injection lives in `src/backend/orchestration/memory_integration.py`; mem0 configuration assembly (LLM / embedder / Milvus / Neo4j / reranker) is in `src/backend/core/memory/service.py` — model configs are resolved from the DB roles `memory` / `embedding` first, falling back to environment variables.

## Write pipeline and extractors

Writes happen only when the user has explicitly enabled `memory_write_enabled` (first gate in `save_memories_background()`, second gate inside `schedule_post_response_tasks()`). Pipeline properties (`core/memory/pipeline.py`):

- **Never awaited**: `schedule_post_response_tasks()` is synchronous and only calls `asyncio.create_task()`;
- **Bounded concurrency**: a global `asyncio.Semaphore` (`MEMORY_BG_MAX_CONCURRENCY`, default 8);
- **Milvus circuit breaker**: after N consecutive failures (default 3) the breaker opens for 60 seconds; retrieval and write paths share the same `milvus_breaker`;
- **Extractor routing** (`core/memory/extractors/router.py`): keyword cues classify each turn, and only matching LLM extractors run — `identity`, `preference`, `fact` (requires an assistant reply > 30 chars), and `task`; an empty classification skips all LLM calls entirely.

## Sanitizer gate

Everything destined for memory storage first passes through `core/memory/sanitizer.py::sanitize()`:

| Category | Behavior | Built-in rule examples |
|---|---|---|
| `CLASSIFIED_TERMS` | **Write rejected** (the whole entry is dropped) | 机密 / 秘密 / 绝密 / 内部资料 / Confidential / NDA, etc. |
| `REDACT_PATTERNS` | Replaced with `[REDACTED:<name>]` but still written | national ID, phone number, email, bank card, API keys, JWTs, official document numbers, customer IDs, intranet URLs |

Rules are runtime-extensible: the DB table `memory_sanitizer_rules` (ORM: `core/db/models/memory.py::MemorySanitizerRule`) supports adding / disabling rules with `rule_type` of `redact` / `classified` / `disable_redact` / `disable_classified`, behind a 5-minute TTL cache; admin changes take effect immediately via `invalidate_rules_cache()`. If the DB is unavailable the system falls back silently to the hardcoded rules.

## Memory auditing (Enterprise Edition, EE)

`core/memory/audit.py` side-records every L1/L2/L3/session read and write into the `memory_audit` table:

- Records actor, action (`read/write/update/delete/write_rejected/forget`), layer, workspace, chat, and confidentiality;
- **Raw content never lands in the audit table** — only a SHA256 `content_hash` is stored;
- Failures never propagate (auditing never blocks the hot path);
- Toggle: `MEMORY_AUDIT_ENABLED` (default `true`).

Per the [edition comparison](../editions/overview.md), memory auditing is a commercial feature flag (`edition_ee/licensing/features.py::Feature.MEMORY_AUDIT`). The audit query endpoint is `GET /v1/memories/audit` (filterable by action / layer).

## Memory management API

Route file: `src/backend/api/routes/v1/memories.py` (registered in the CE router table).

| Method | Path | Description |
|---|---|---|
| GET | `/v1/memories` | L2 fact list; `?project_id=` filters by project workspace |
| GET | `/v1/memories/profile` | L1 profile (full markdown + char cap) |
| GET | `/v1/memories/graph` | L3 graph (currently returns enabled status; structured relation queries planned) |
| GET | `/v1/memories/audit` | Audit records (Enterprise Edition, EE) |
| GET | `/v1/memories/settings` | Read user memory / reranker toggles |
| PATCH | `/v1/memories/settings` | Update toggles (persisted to `users_shadow.metadata`) |
| DELETE | `/v1/memories` | Clear all of the current user's L2 memories |
| DELETE | `/v1/memories/{id}` | Delete a single L2 memory |

## User toggles and scoping

Two independent toggles, both stored in `users_shadow.metadata` (ORM column `extra_data`):

| Toggle | Meaning | Default |
|---|---|---|
| `memory_enabled` | Persistent memory **read**: inject the frozen block at session start | `false` |
| `memory_write_enabled` | **Write**: extract and save memories after each conversation | `false` |

Project chats get their own scope: personal projects and the default workspace use the real `user_id`, while team projects use `scope_user_id = "team:<team_id>"` — all team members write into the same mem0 bucket for shared recall, with the real author preserved in `metadata.author_user_id` (see `orchestration/memory_integration.py::save_memories_background` and `api/routes/v1/memories.py::list_memories`). Project-level toggles live in `projects.metadata` (`memory_enabled` / `memory_write_enabled`, defaulting to `true` inside a project); see [Projects & MySpace](./projects-myspace.md).

## Frontend memory center

- Entry point: the "Memory settings" section of the settings modal (`src/frontend/src/components/settings/SettingsModal.tsx`), with two switches: "Write memory" and "Persistent memory";
- The "My layered memory" modal has three tabs — Profile L1 (full markdown), Facts L2 (list with per-item delete and clear-all, component `src/frontend/src/components/memory/FactsList.tsx`), and Graph L3 (shows a hint to configure `MEM0_GRAPH_ENABLED` + Neo4j when disabled);
- Project-scoped memory viewing: `src/frontend/src/components/projects/ProjectMemoriesModal.tsx`;
- API client wrappers: `src/frontend/src/api.ts` (`getMemories` / `getMemoryProfile` / `getMemoryGraph` / `getMemorySettings`, etc.).

## Infrastructure

The vector and graph stores backing L2/L3 are started with the Docker Compose `mem0` profile (when disabled, the main app short-circuits all memory paths at zero cost):

```bash
docker-compose --profile mem0 up -d
```

| Service | Image | Role |
|---|---|---|
| milvus | `milvusdb/milvus:v2.4.0` (standalone) | L2 vector store |
| etcd | `quay.io/coreos/etcd:v3.5.5` | Milvus metadata |
| minio | `minio/minio` | Milvus object storage |
| neo4j | `neo4j:5.15-community` | L3 graph store (optional) |

See [Docker Compose deployment](../deployment/docker-compose.md).

## Environment variables

```bash
# Master switches
MEM0_ENABLED=true                 # default false; when false, all memory paths short-circuit
MEM0_GRAPH_ENABLED=false          # L3 graph (requires Neo4j)

# Embedding service (memory vectors)
MEM0_EMBED_URL=http://<embed-host>/v1
MEM0_EMBED_MODEL=qwen3_embedding_8b
MEM0_EMBED_API_KEY=sk-...
MEM0_EMBED_DIMS=1024

# LLM used for memory extraction (falls back to MODEL_URL / API_KEY / BASE_MODEL_NAME)
MEMORY_MODEL_NAME=...
MEMORY_MODEL_URL=...
MEMORY_API_KEY=...

# Stores
MILVUS_URL=http://milvus:19530
MILVUS_TOKEN=
NEO4J_URL=bolt://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=...

# Behavior tuning (all have sensible defaults)
MEMORY_LAYERED_ENABLED=true       # layered memory
MEMORY_AUDIT_ENABLED=true         # audit side channel (Enterprise Edition, EE)
MEMORY_RETRIEVAL_BUDGET_MS=600    # retrieval budget
MEMORY_BG_MAX_CONCURRENCY=8       # background write concurrency
MEMORY_EXTRACT_TIMEOUT_S=30       # per-extractor timeout
MEMORY_PROFILE_MAX_CHARS=1500     # L1 profile char cap
MEMORY_FACT_DEFAULT_TTL_DAYS=180  # default L2 fact TTL
MEMORY_FROZEN_TOPK=5              # frozen-block fact Top-K
MEMORY_BREAKER_THRESHOLD=3        # Milvus breaker threshold
MEMORY_BREAKER_COOLDOWN_S=60      # breaker cooldown

# Optional: retrieval reranking
RERANKER_URL=...
RERANKER_MODEL=...
RERANKER_API_KEY=...
```

See the [environment variable reference](../deployment/environment-variables.md) for the full list. Settings are defined in `src/backend/core/config/settings.py::MemorySettings`.

## Source map

| Path | Responsibility |
|---|---|
| `src/backend/core/memory/__init__.py` | Layered memory package entry and public API |
| `src/backend/core/memory/service.py` | mem0 config assembly and async wrappers (Milvus / Neo4j / reranker) |
| `src/backend/core/memory/profile.py` | L1 profile: get / patch / compact / delete |
| `src/backend/core/memory/pipeline.py` | Post-response write pipeline, semaphore, Milvus circuit breaker |
| `src/backend/core/memory/extractors/` | identity / preference / fact / task extractors + keyword router |
| `src/backend/core/memory/sanitizer.py` | Sanitizer gate (hardcoded + DB-managed rules) |
| `src/backend/core/memory/audit.py` | Audit side channel (Enterprise Edition, EE) |
| `src/backend/core/memory/context.py` | `MemoryContext` and workspace / layer resolution |
| `src/backend/orchestration/memory_integration.py` | Retrieval launch, frozen-block assembly and injection, save delegation |
| `src/backend/orchestration/workflow.py` | Main orchestration: memory hook wiring |
| `src/backend/api/routes/v1/memories.py` | `/v1/memories` management API |
| `src/backend/core/db/models/memory.py` | Shared `MemorySanitizerRule` ORM |
| `src/backend/edition_ee/db/models/memory.py` | `MemoryAudit` ORM (EE only) |
| `src/frontend/src/components/settings/SettingsModal.tsx` | Memory settings + layered memory modal |
| `src/frontend/src/components/memory/FactsList.tsx` | L2 fact list component |
| `docker-compose.yml` (`mem0` profile) | Milvus / etcd / MinIO / Neo4j |
