# Capability Center (Catalog)

> Last updated: 2026-06-11

The capability catalog is the **single source of truth** for "what is available" in HugAgentOS: skills, sub-agents (agents), MCP tools (mcp), and knowledge bases (kb) are all registered in the catalog, which gates everything from MCP tool loading and system-prompt assembly to the frontend Capability Center. On top of the system-level defaults, every user has a personal override layer (catalog_overrides) and self-added private capabilities (owner-isolated).

## Architecture

```
core/config/catalog.json            system-level single source of truth (admin toggles + display order)
        │  load_catalog(): TTL cache + dynamic-source sync (skill/MCP discovery, stale-item removal)
        ▼
core/config/catalog.py              public API: get_catalog / is_enabled / get_enabled_ids
        │                                       set_enabled / reorder_items
        ▼
core/config/catalog_resolver.py     merge layer: catalog.json defaults ∩ per-user DB overrides
        │  resolve_all_runtime_enabled(db, user_id) → (skills, agents, mcps)
        ▼
Consumed along the request path:
  api/routes/v1/chats.py → core/chat/context.py   written into the workflow context
  core/llm/agent_factory.py                       decides which MCP servers to connect / skills to register
  api/routes/v1/catalog.py                        /v1/catalog for the frontend Capability Center
```

### catalog.json: the single source of truth

The file lives at `src/backend/core/config/catalog.json` (path overridable via the `CATALOG_PATH` env var). The minimal auditable schema per item:

```json
{
  "id": "internet_search",
  "kind": "mcp_server",          // tool_bundle | subagent | mcp_server | knowledge_base
  "name": "Web Search",
  "description": "…",
  "enabled": true,
  "version": "1",
  "config": {}
}
```

`catalog_loader.py` performs **dynamic-source synchronization** at load time: new ids discovered from the skill loader (`core/agent_skills/`) and the MCP config service (`core/services/mcp_service.py`, DB table `admin_mcp_servers`) are added automatically; names/descriptions/versions refresh from the source; stale entries whose id no longer exists in any source are removed. Display details (Capability Center intro markdown, icons) are runtime-only fields and never persisted. Results are held in a short-TTL in-memory cache; writes call `invalidate_catalog_cache()`.

### Gating API

```python
from core.config.catalog import is_enabled, get_enabled_ids

is_enabled("mcp", "internet_search")   # single-item check
get_enabled_ids("mcp")                 # all enabled ids of a kind
```

These two functions are the system-wide read path for capability switches: `agent_factory.py::_effective_mcp_server_keys()` intersects "all enabled DB servers ∩ request-level enabled_mcp_ids (falling back to `get_enabled_ids("mcp")`) ∩ AgentSpec allowlist" to decide which MCP servers to connect for a request; skill registration defaults to `get_enabled_ids("skills")`. Catalog edits take effect on the next request — no restart.

## Per-user overrides (catalog_overrides)

When a user toggles a capability in the Capability Center, the change is written to the `catalog_overrides` DB table (`core/services/catalog_service.py`), per-user and isolated:

- **Merge algorithm** (`catalog_resolver.py::_merge_kind`): overrides can only flip the enabled flag of items that *exist* in the base catalog; they can never resurrect deleted items.
- **Admin lock**: items disabled in the base catalog (`enabled=false`) **cannot** be re-enabled by user overrides, and are completely hidden from the user-facing `/v1/catalog` response.
- Resolution results are cached per user_id for 30 seconds (`resolve_all_runtime_enabled`).

On every chat, `core/chat/context.py::resolve_enabled_capabilities()` writes the merged result into the workflow context (explicit lists in the request body take precedence).

## The /v1/catalog route and KB injection

`api/routes/v1/catalog.py`:

- `GET /v1/catalog`: returns the **user's view** — base + user overrides + the user's private items — consumed by `src/frontend/src/api.ts::getCatalog()`.
- `PATCH /v1/catalog/{kind}/{id}`: writes a user override (kind ∈ `skill / agent / mcp / kb`; kb toggles are runtime-only and not persisted). Skill/tool changes cascade-invalidate the system-prompt cache.

**KB items are injected at runtime** and never persisted in catalog.json:

| Source | Condition | Marking |
|---|---|---|
| Dify external KB | `KNOWLEDGE_BASE=dify` with valid credentials (`core/kb/dify_kb.py::is_dify_enabled`); dataset list cached 60 s in-process | `visibility: public` (**Enterprise Edition (EE)**: external Dify KB integration) |
| Public self-hosted KB | created in the admin "KB management" console (local Milvus); visible to all users, read-only on the frontend | `visibility: public` |
| Private user KB | the current user's local KB spaces | `visibility: private` |

See [Knowledge Base](knowledge-base.md).

## User self-service capabilities (me_capabilities)

`api/routes/v1/me_capabilities.py` lets users plug capabilities in **without an administrator** — a core self-service path of the Community Edition. All self-created items carry `owner_user_id = current user` and are visible/usable only to their owner:

| Endpoint | Function |
|---|---|
| `POST /v1/me/mcp-servers` | Add a private remote MCP (only `streamable_http` / `sse`; **stdio is not supported** — users must not run arbitrary commands on the server). Connectivity is probed at creation; unreachable endpoints are rejected |
| `DELETE /v1/me/mcp-servers/{id}` | Delete one's own private MCP |
| `POST /v1/me/skills/upload` | Upload a private skill as a zip (≤50 MB, reusing the admin parsing pipeline) |
| `POST /v1/me/skills` | Create/update a private skill by hand (SKILL.md body + metadata) |
| `GET/DELETE /v1/me/skills/{skill_id}`, `PUT .../icon` | Read for editing, delete, set icon |

**Permission flags**: each endpoint first checks a boolean in `users_shadow.metadata` — `can_add_mcp`, `can_add_skill` (personal API keys use the same mechanism with `can_use_api_key`, see [Model Providers](model-providers.md)). These flags are granted per user from the Config console's user management. In the single-tenant Community Edition they are simply switched on for full self-service; **per-user governance of these flags and skill review/approval belong to the Enterprise Edition (EE)**.

Runtime isolation: `agent_factory.py` merges the current user's private MCP servers into the connectable set (`get_owned_servers`) and strips other users' private skill ids via `_filter_skill_ids_for_user()` to prevent privilege escalation; `/v1/catalog` injects private items into the owner's response with `owner: "self"` and `deletable: true`.

## Frontend Capability Center (components/catalog/)

| Component | Responsibility |
|---|---|
| `AbilityCenterPage.tsx` | Capability Center main page: browse the four kinds, details, toggles, self-service entry points |
| `CatalogPanel.tsx` | In-chat capability panel (which skills/tools/KBs are enabled for this conversation) |
| `McpPage.tsx` / `SkillsPage.tsx` | MCP / skill pages and management |
| `SkillMarketplaceModal.tsx` | Skill marketplace browsing & installation (see [Agent Skills](agent-skills.md)) |
| `SkillIconPicker.tsx` / `skillIcons.tsx` | Icon picker and presets |

State is centralized in `src/frontend/src/stores/catalogStore.ts`; local defaults are in `storage.ts::defaultCatalog`.

## Admin-side capability management

- **`/admin` content console**: skill upload/toggle/reorder (`api/routes/v1/admin_skills.py`, which calls `catalog.set_enabled` / `reorder_items` to write back to catalog.json), MCP server management (`admin_mcp_servers.py`, DB table + connectivity probe + cache refresh), sub-agent management (`admin_agents.py`), and skill marketplace listing review (`admin_marketplace.py`, **Enterprise Edition (EE)**).
- **Catalog snapshot migration**: `scripts/export_content.py --only catalog` exports catalog.json + `catalog_overrides`; `scripts/import_content.py --catalog <snapshot>` imports them.

## Source map

| Topic | Path |
|---|---|
| Public API (is_enabled, etc.) | `src/backend/core/config/catalog.py` |
| Single-source file | `src/backend/core/config/catalog.json` (overridable via `CATALOG_PATH`) |
| Loading / dynamic sync / caching | `src/backend/core/config/catalog_loader.py`, `catalog_common.py`, `catalog_migration.py` |
| User-override merging | `src/backend/core/config/catalog_resolver.py`, `core/services/catalog_service.py` |
| Catalog route | `src/backend/api/routes/v1/catalog.py` |
| User self-service capabilities | `src/backend/api/routes/v1/me_capabilities.py` |
| MCP server config (DB) | `src/backend/core/services/mcp_service.py`, `api/routes/v1/admin_mcp_servers.py` |
| Skill management | `src/backend/api/routes/v1/admin_skills.py`, `core/agent_skills/` |
| Dify KB injection | `src/backend/core/kb/dify_kb.py` |
| Frontend Capability Center | `src/frontend/src/components/catalog/`, `stores/catalogStore.ts` |
| Factory consumption | `src/backend/core/llm/agent_factory.py::_effective_mcp_server_keys` |
