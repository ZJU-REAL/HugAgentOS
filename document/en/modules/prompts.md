# Prompt System

> Last updated: 2026-06-11

System prompts in HugAgentOS are not hardcoded strings — they form a **DB-first, file-fallback, versioned** assembly system: at runtime, the main agent's prompt is assembled from the parts of the active version stored in the database; administrators maintain multiple versions in the Config console and activate them with one click; the whole thing can be exported as a snapshot and migrated across environments. Markdown files on disk serve only as the first-deployment seed and the fallback when the DB is unavailable.

## Assembly (prompts/prompt_runtime.py)

`build_system_prompt(config, ctx)` is the single entry point for the main agent's system prompt, called by `core/llm/agent_factory.py` on every agent build. Resolution priority:

```
1. Active pool version       the active kind="system" version inside ContentBlock(id="prompt_versions")
   (rows in the AdminPromptPart table may override individual parts by part_id,
    for backward compatibility with the older admin UI)
2. Filesystem parts          prompts/prompt_text/default/system/*.system.md
   (provider="filesystem"; directory overridable via the PROMPT_DIR env var)
3. Inline template           provider="inline" / PROMPT_INLINE_TEMPLATE
4. Hardcoded minimal prompt  prompts/provider.py::hardcoded_minimal_system_prompt()
                             — guarantees a non-empty prompt
```

Fallback parts (used when the DB is empty) are 5 files, concatenated in filename order:

```
src/backend/prompts/prompt_text/default/system/
├── 00_role.system.md          # role definition
├── 10_constraints.system.md   # anti-hallucination hard constraints
├── 20_tools.system.md         # tool-usage policy
├── 30_workflow.system.md      # workflow
└── 40_format.system.md        # output format + [ref:tool-N] citation contract
```

After the base prompt, runtime appends dynamic sections per context: the tools & skills notice (`_TOOLS_AND_SKILLS_NOTICE`), the lightweight KB catalog (`prompts/kb_lite_section.py`), the project-mode section (`prompts/project_section.py`, injected only for project chats), the code-execution segment and batch-mode hint (appended by `agent_factory.py`), and the sub-agent routing table (`core/llm/subagent_tool.py::build_subagent_prompt_section`).

### Caching

Prompt assembly uses three cache layers, all actively invalidatable:

| Cache | TTL | Notes |
|---|---|---|
| Template cache `_prompt_cache` | 300 s | key includes provider, parts, tool-name set, MCP keys, DB version, active version `(id, updated_at)`, project signature, etc.; `{now}` is stored as a placeholder and rendered as a **day-granularity** date — the system prompt stays byte-stable all day, maximizing LLM prefix-cache hits |
| DB parts preload `_db_parts_preloaded` | preloaded at startup via `warmup_prompt_cache()`, reloaded after writes | first request never queries the DB |
| DB version `_db_version_cache` | 30 s | `MAX(admin_prompt_parts.updated_at)` as a cache-busting version string |

Any prompt write (console edit, version activation, snapshot import, capability toggle) calls `invalidate_prompt_cache()`, which cascades and immediately re-warms.

## Prompt version pool (prompt_versions)

The pool stores multiple prompt sets in a single `ContentBlock(id="prompt_versions")` row with payload `{active: {kind: version_id}, versions: [...]}`; the service layer is `core/services/prompt_version_service.py`:

- **Four kinds** (`VALID_KINDS`): `system` (main agent), `code_exec` (code-execution segment), `distillation` (skill distillation), `plan_mode` (plan mode).
- Each version carries `(kind, id, name, description, parts[])`, a part being `{part_id, display_name, content, sort_order, is_enabled}`.
- **API**: `list_versions / get_version / upsert_version (with from_id cloning) / delete_version (active version cannot be deleted) / activate_version`; activation immediately invalidates runtime caches.
- **Seeding**: `seed_from_filesystem()` turns the on-disk markdown into default versions on cold start; it also runs two one-time migrations — renaming `system/v4 → system/default`, and extracting `system/90_plan_mode` out of system versions into a new `plan_mode/default` version.
- Startup also idempotently seeds two dynamic parts into the active system version: `system/05_system_reminder_convention` (teaches the model how to handle out-of-band `<system-reminder>` signals) and the project-mode part (`prompt_runtime.py::ensure_*_seeded`).

### Config console

Management lives under "Prompt Management" in the Config console, backed by `api/routes/v1/admin_prompts.py` (`CONFIG_TOKEN` auth):

| Endpoint | Function |
|---|---|
| `GET/POST/PUT/DELETE /v1/admin/prompts/versions...` | Pool CRUD (per kind) |
| `POST /v1/admin/prompts/versions/{kind}/{id}/activate` | Activate a version |
| `GET/PUT/DELETE /v1/admin/prompts/parts/{part_id}` | Edit parts of the active version |
| `PUT /v1/admin/prompts/order` | Reorder parts |
| `POST /v1/admin/prompts/preview` | Preview the *actual* runtime assembly (including the code-execution segment and tool appendix — identical to what the agent sees) |
| `GET/POST /v1/admin/prompts/export` / `import` | Part-level export/import |

## Scenario prompts

| kind | Runtime consumer | Resolution order |
|---|---|---|
| `code_exec` | `agent_factory.py` appends this segment (the code-capability prompt) to the system prompt when `CODE_CAPABILITY_ENABLED=true`; the single source of truth is `prompt_version_service.render_code_capability_segment()`, also used by the console preview | active DB version → `prompts/prompt_text/code_exec/system/*.system.md` |
| `distillation` | Skill distillation (`core/llm/skill_distiller.py`, distilling conversation trajectories into reusable skills) | active DB version → `prompts/prompt_text/distillation/skill_distiller.system.md` |
| `plan_mode` | The plan-generation sub-agent (`orchestration/subagents/plan_mode.py::_load_plan_prompt`) | active `plan_mode` DB version → legacy `system/90_plan_mode` part → `prompts/prompt_text/plan_mode/plan_mode.system.md` → hardcoded fallback |

Sub-agents do not use the full pool assembly: `prompt_runtime.py::build_subagent_system_prompt()` builds around the user-defined `system_prompt`, reusing the `20_tools_policy` / `65_citations` / `60_format` parts from the active version (or files). See [Chat & Agent Orchestration](chat.md).

## Prompt Hub (prompt_hub)

The Prompt Hub is an end-user template gallery stored in `ContentBlock(id="prompt_hub")`:

- **Frontend read**: `GET /v1/content/docs` (no auth) returns the `prompt_hub` list; `src/frontend/src/components/chat/PromptHubPanel.tsx` renders it in the input area for one-click insertion.
- **Admin write**: `PUT /v1/content/docs/prompt_hub` (`ADMIN_TOKEN`), edited via `src/frontend/src/components/admin/PromptHubEditor.tsx`.

## Cross-environment migration

Prompts live only in the database and never ship with code; migration across environments (dev → staging → production) relies on snapshots:

### HTTP endpoints (api/routes/v1/content.py)

| Endpoint | Description |
|---|---|
| `GET /v1/content/prompts/export` | Export the `prompt_versions` + `prompt_hub` blocks as a snapshot JSON (decoupled from `page_config`, so no branding fields ride along) |
| `POST /v1/content/prompts/import?overwrite=true` | Import a snapshot; **caches for** `prompt_version_service` and `prompt_runtime` **are invalidated automatically** — no backend restart needed |

Both accept `ADMIN_TOKEN` or `CONFIG_TOKEN`. Snapshots are validated against `PROMPT_BLOCK_MAP`, so docs snapshots and prompt snapshots cannot be imported through the wrong endpoint.

### Scripts (src/backend/scripts/)

```bash
# Export (via a running backend API; --database-url for direct DB access also works)
python scripts/export_content.py --api-url http://localhost:3000/api --only prompts
# → scripts/exported/prompts_snapshot_<ts>.json

# Import into the target environment (use the target machine's ADMIN_TOKEN)
python scripts/import_content.py --api-url http://<HOST>/api --prompts prompts_snapshot_<ts>.json
# supports --no-overwrite / --dry-run
```

The same flow works for offline production (image-pack delivery, persistent DB volume): copy the snapshot in alongside the image pack and `curl -X POST .../v1/content/prompts/import` inside the backend container — no restart required. When migrating to a differently-branded environment, review the snapshot manually and rewrite brand-specific wording in context before importing (never do mechanical find-and-replace).

## Source map

| Topic | Path |
|---|---|
| Runtime assembly + caching | `src/backend/prompts/prompt_runtime.py` |
| Providers (filesystem/inline/minimal) | `src/backend/prompts/provider.py` |
| Config (provider/parts/PROMPT_DIR) | `src/backend/prompts/prompt_config.py`, `prompts/config/default.json` |
| Version pool service | `src/backend/core/services/prompt_version_service.py` |
| Console routes | `src/backend/api/routes/v1/admin_prompts.py` |
| Migration endpoints (export/import) | `src/backend/api/routes/v1/content.py`, `core/content/content_blocks.py` |
| Migration scripts | `src/backend/scripts/export_content.py`, `scripts/import_content.py` |
| System prompt fallback files | `src/backend/prompts/prompt_text/default/system/` |
| Scenario prompt fallbacks | `src/backend/prompts/prompt_text/{code_exec,distillation,plan_mode}/` |
| Dynamic sections | `src/backend/prompts/kb_lite_section.py`, `prompts/project_section.py` |
| Prompt Hub frontend | `src/frontend/src/components/chat/PromptHubPanel.tsx`, `components/admin/PromptHubEditor.tsx` |
