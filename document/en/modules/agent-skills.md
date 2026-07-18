# Agent Skill System

> Last updated: 2026-06-11

A skill is HugAgentOS's standard vehicle for "teaching the model a workflow": one skill = one directory whose core is a `SKILL.md` playbook with YAML frontmatter, plus any number of scripts, templates and reference assets. Skills follow **progressive disclosure** — the system prompt only carries each skill's name, description and directory path; the model reads the full playbook on demand and then drives the sandbox to execute the skill's scripts.

Division of labour with [MCP tools](mcp-tools.md): MCP servers are *programmatic atomic capabilities* (one call, one function), skills are *knowledge-encoded workflows* (teaching the model to compose bash, file tools and MCP calls into complex jobs). The office suite (Word/Excel/PPT/PDF) is the canonical example of a capability migrated from MCP form to skill form — each skill vendors its own CLI engine and runs inside the sandbox.

## Anatomy of a skill

```
<skill-id>/
├── SKILL.md          # required: frontmatter (name/description/version/tags) + playbook body
├── scripts/…         # optional: executable scripts (.py/.js/.sh/.r)
├── reference/…       # optional: reference docs, templates, fonts, any assets
└── _scripts.json     # optional: script whitelist (auto-detected by extension when absent)
```

Parsing and data structures live in `core/agent_skills/registry.py`: `AgentSkillMetadata` (lightweight, for listings) and `AgentSkillSpec` (full instructions, for execution). When `_scripts.json` is missing, `loader._auto_detect_scripts` builds the whitelist from file extensions automatically.

## Multi-source loading and priorities

`core/agent_skills/loader.py::MultiSourceSkillLoader` loads skills from several sources through a backend abstraction (`backends/`: filesystem / database / composite); on ID conflicts the higher-priority source wins (`core/agent_skills/config.py`):

| Source | Directory | Priority | Notes |
|---|---|---|---|
| built-in | `src/backend/skill_bundles/default/` | 0 | Ships with the repo, always on |
| user | `~/.hugagent/skills/` (`HUGAGENT_USER_SKILLS_DIR`) | 50 | Filesystem user skills |
| admin | `/app/storage/admin_skills/` (`HUGAGENT_ADMIN_SKILLS_DIR`) | 75 | Managed via the admin console (DB-stored + materialized) |
| project | `.hugagent/skills/` (`HUGAGENT_PROJECT_SKILLS_DIR`) | 100 | Project-level overrides |

Each source can be disabled individually via `HUGAGENT_DISABLE_{ADMIN,USER,PROJECT}_SKILLS=1`.

## skill_bundles layering: default vs marketplace

`src/backend/skill_bundles/` has two tiers with very different loading semantics:

- **`default/` — 5 built-in skills** (always-on; the built-in loader's single-level `glob("*/SKILL.md")` scan picks them up):

  | Skill | Purpose |
  |---|---|
  | `capability-guide-brief` | Quick answers to "what can you do" questions |
  | `word-editing` | Word document creation/editing/templating (word-cli) |
  | `excel-editing` | Excel workbook creation / formula modelling (excel-cli) |
  | `ppt-design` | PPT design and generation (.pptx deliverables) |
  | `pdf-editing` | PDF creation/merge/split/form-filling |

- **`marketplace/` — 48 installable skill packages** (install-to-use): each directory holds the raw SKILL.md, referenced files and a `marketplace.json` manifest. Because they sit two levels deep at `marketplace/<slug>/SKILL.md`, the built-in loader **never** auto-loads them — before installation they don't appear in the catalog and aren't registered with agents; only an explicit install persists them.

  Ten of these are industry/brand skills (economic-indicator query, enterprise profiling, industry-chain analysis, etc.) hard-wired to the [EE industry MCP servers](mcp-tools.md) and are therefore **Enterprise EE**; the CE derivation strips them via `ce/manifest.yaml`.

## Injection: how skills enter the prompt

Skill registration happens while `core/llm/agent_factory.py` builds the agent. The enabled set comes from the [catalog](catalog.md) `skills` section plus the user/sub-agent configuration (private skills are filtered by `owner_user_id` to prevent cross-user leakage), then each skill is registered through AgentScope's `toolkit.register_agent_skill(skill_dir)`. AgentScope renders a skills section into the system prompt — name, description and the `{dir}` directory path per skill.

A crucial path redirection follows (`loader._repoint_skill_dir_to_sandbox`): the registered directory is a **backend physical path** (built-ins in the source tree, DB skills materialized under `/app/storage/sandbox_skills/<id>`), but scripts actually execute in the **sandbox**, where every skill lives at the unified path `/workspace/skills/<id>`. The prompt-facing `dir` is rewritten to that sandbox path immediately after registration — otherwise the model would feed backend paths to `bash` and be rejected by the path validator.

The model reads SKILL.md through the restricted `view_text_file` tool (`core/llm/tools/skill_tool.py::register_sandboxed_view_text_file` — reads are confined to skill directories; sandbox paths are mapped back to backend files automatically). On reading a SKILL.md the system:

1. substitutes the `{baseDir}` placeholder in the body with the actual directory;
2. appends a Runtime Hint telling the model the skill files are ready at `/workspace/skills/<id>/`, how to run scripts via `bash`, and how to exchange files via `sandbox_put_artifact` / `sandbox_get_artifact`.

Execution flow:

```
user request ──▶ system prompt (skill name+description+/workspace/skills/<id>)
                  │  model decides the skill applies
                  ▼
          view_text_file(SKILL.md)        ← {baseDir} substitution + Runtime Hint
                  │  composes commands per the playbook
                  ▼
          bash("cd /workspace/skills/<id> && python scripts/foo.py …")
                  │  executes in the sandbox (skills dir mounted read-only)
                  ▼
          sandbox_get_artifact(output path) → downloadable for the user
```

### How skill files reach the sandbox

All skills — built-in and DB/admin-imported — are exposed inside the sandbox through a **single read-only host bind mount** at `/workspace/skills/<id>` (details in the [sandbox module](sandbox.md)):

- the unified host directory is resolved by `core/agent_skills/config.py::get_sandbox_skills_dir()` (default `$STORAGE_PATH/sandbox_skills`, overridable via `SANDBOX_SKILLS_DIR`);
- at backend startup `sync_builtin_skills_to_sandbox_dir()` copies built-ins into it (idempotent overlay, so edits propagate on restart);
- DB skills are materialized into the same directory on demand (`loader._materialize_skill_files`);
- the remote cube sandbox has no host mounts, so skill files matching `/workspace/skills` are pushed into the sandbox at runtime instead (`CUBE_SKILL_PREPUSH*` settings).

## The Skill Marketplace

The marketplace is an installable skill library with two sources — curated presets and community submissions — implemented in `core/services/marketplace_service.py`.

### Browsing and installing

| Endpoint | Description |
|---|---|
| `GET /v1/marketplace/skills` | Marketplace listing (preset manifest + approved community skills, annotated with the current user's install state) |
| `GET /v1/marketplace/categories` | The 8 fixed categories: writing, document processing, data analysis, policy & industry, marketing, legal & compliance, office productivity, engineering productivity |
| `GET /v1/marketplace/skills/{slug}` | Details (SKILL.md preview, declared required_secrets) |
| `POST /v1/marketplace/install` | User install → **private skill** |
| `POST /v1/admin/marketplace/install` | Admin install → **global skill** |

**Installing = creating an `AdminSkill` row**, fully reusing the existing admin-skill machinery:

- admin install: empty `owner_user_id` (available to everyone), skill id = the manifest's `entry_name`;
- user install: `owner_user_id` = current user, skill id suffixed with a user fingerprint (`compute_install_id`) for global uniqueness — multiple users can install the same marketplace skill, each with their own credentials.

### Credentials: required_secrets → secrets.json

A `marketplace.json` may declare `required_secrets` (e.g. API keys for third-party search/image services). At install time the frontend collects the user's values and `_inject_secrets` writes them into a `secrets.json` inside **that install's** skill directory, appending a "credential configuration" section to the SKILL.md so scripts know where to read them. The marketplace directory itself **never stores any secret**.

### Community publishing: submit → review → publish/unpublish

Users can apply to publish their private skills (the `marketplace_submissions` table):

```
user   POST /v1/marketplace/submissions       ← snapshots the source skill's content
       GET  /v1/marketplace/submissions          (decoupled from later edits); injected
       DELETE /v1/marketplace/submissions/{id}    credential sections are stripped, and
                     │                            required_secrets travel in a sentinel
                     ▼                            file _required_secrets.json
admin  GET  /v1/admin/marketplace/submissions            (/admin review console)
       POST /v1/admin/marketplace/submissions/{id}/approve → listed as source=community
       POST /v1/admin/marketplace/submissions/{id}/reject  → rejected; rejecting an
                                                             approved submission unpublishes it
```

Reviewers may correct the category (restricted to the 8 fixed values). The published snapshot is the **pre-injection** form — every installer supplies their own keys.

### Marketplace visibility scope (EE)

Items in the skill, plugin, and sub-agent marketplaces support scoped delivery: the default is `public` (visible to everyone), and admins can switch an individual item to `scoped` in the corresponding `/admin` marketplace UI, whitelisting principals of three kinds — **roles / teams / users** — where matching any one grants visibility (union semantics). The scope only affects marketplace browsing and installation (list, detail, and install endpoints share the same filter); it never retroactively affects already-installed instances, and admins can always see everything.

```
admin  GET/PUT /v1/admin/marketplace/skills/{slug}/visibility          (skill marketplace)
       GET/PUT /v1/admin/plugins/market/{slug}/visibility              (plugin marketplace)
       GET/PUT /v1/admin/agent-marketplace/agents/{slug}/visibility    (sub-agent marketplace)
       GET     /v1/admin/visibility/principals    ← brief user/team/role lists (picker data source)
```

Storage: `marketplace_listing_states.visibility` (missing row = public) plus the `marketplace_visibility_grants` whitelist table; user-side resolution is centralized in `core/auth/marketplace_visibility.py` (roles include both direct assignments and team-default roles inherited via team membership).

## Admin skill management

The `/admin` console's skill management maps to `api/routes/v1/admin_skills.py` (prefix `/v1/admin/skills`), covering the full lifecycle:

- **Create/edit**: hand-written SKILL.md (`POST /`, `PUT /{id}`) or **zip upload** of an entire package (`POST /upload`, auto-detects directory prefixes, base64-encodes binaries into the DB, 200 MB cap);
- **File-level management**: `GET/PUT/DELETE /{id}/files/{filename}` for inline editing of auxiliary files;
- **Operations**: toggle (`/{id}/toggle`), ordering, icons, fork, bulk export/import (cross-environment migration);
- **Dependency management**: `POST /{id}/rescan-deps` statically scans scripts for pip/apt dependencies via `core/agent_skills/deps_detector.py`, with `PUT /{id}/dependencies` for manual correction; the aggregated manifest feeds the sandbox image rebuild (see the admin dependency rebuild in the [sandbox module](sandbox.md)).

## Skill distillation (Enterprise EE)

The platform can **auto-distill candidate skills from past conversations**:

```
daily cron (default 02:30, DISTILL_CRON_EXPRESSION)
  → orchestration/schedulers/distillation_cron_scheduler.py
     scans yesterday's active chats (Redis day-lock against duplicate instances)
     → inserts distillation_runs
  → core/llm/skill_distiller.py
     trajectory pre-check → strict-JSON LLM distillation
     (model role 'skill_distiller', falling back to 'main_agent')
     → decision = new_skill / patch → writes admin_skill_drafts (with cost records)
  → admins review drafts in /admin (api/routes/v1/admin_skill_drafts.py)
     approve → promoted to AdminSkill; reject / delete; manual trigger-daily-scan available
```

Thresholds, keywords and budgets are all overridable via `DISTILL_*` environment variables (`core/config/distillation.py`). The distillation pipeline (`skill_distiller.py`, the scheduler) is **Enterprise EE**; the CE tree strips it, and the draft-review console only appears in the Enterprise Edition.

## User self-service skills (capability center)

Regular users (gated by the `can_add_skill` permission flag granted in the admin console) can create **private** skills visible only to themselves (`api/routes/v1/me_capabilities.py`):

| Endpoint | Description |
|---|---|
| `POST /v1/me/skills/upload` | Upload a skill zip (50 MB cap, smaller than the admin's 200 MB) |
| `POST /v1/me/skills` | Hand-written creation (SKILL.md content submitted directly) |
| `GET /v1/me/skills/{id}` | Fetch for editing |
| `PUT /v1/me/skills/{id}/icon` | Set an icon |
| `DELETE /v1/me/skills/{id}` | Delete |

Private skills land in the same `AdminSkill` table (`owner_user_id` = the user); at runtime `agent_factory._filter_skill_ids_for_user` guarantees they never leak to others. Once a private skill matures, the community-publishing flow above brings it to the marketplace.

## Source map

| Path | Description |
|---|---|
| `src/backend/core/agent_skills/registry.py` | SKILL.md parsing, metadata / full-spec dataclasses |
| `src/backend/core/agent_skills/loader.py` | Multi-source loading, DB-skill materialization, sandbox path repointing |
| `src/backend/core/agent_skills/config.py` | Source priorities, unified sandbox skills dir, built-in sync |
| `src/backend/core/agent_skills/selector.py` | LLM-based skill selection per user intent (progressive disclosure) |
| `src/backend/core/agent_skills/deps_detector.py` | Static pip/apt dependency detection for scripts |
| `src/backend/core/agent_skills/backends/` | filesystem / database / composite loading backends |
| `src/backend/core/llm/tools/skill_tool.py` | Restricted view_text_file + {baseDir} substitution + Runtime Hint |
| `src/backend/skill_bundles/default/` | The 5 built-in skills |
| `src/backend/skill_bundles/marketplace/` | The 48 installable marketplace packages |
| `src/backend/core/services/marketplace_service.py` | Marketplace listing / install / secret injection / submission review |
| `src/backend/api/routes/v1/marketplace.py` | User-side marketplace API (browse/install/submit/withdraw) |
| `src/backend/api/routes/v1/admin_marketplace.py` | Admin marketplace API (global install / submission review) |
| `src/backend/api/routes/v1/admin_skills.py` | Admin skill CRUD / zip / dependency management |
| `src/backend/api/routes/v1/admin_skill_drafts.py` | Distillation draft review (EE) |
| `src/backend/core/llm/skill_distiller.py` | Skill distillation LLM pipeline (EE) |
| `src/backend/orchestration/schedulers/distillation_cron_scheduler.py` | Daily distillation scheduler (EE) |
| `src/backend/api/routes/v1/me_capabilities.py` | User self-service skill / private MCP API |

Related docs: [Sandbox execution](sandbox.md) · [MCP tool system](mcp-tools.md) · [Capability catalog](catalog.md) · [Admin console](admin-console.md) · [Editions & licensing](../editions/overview.md)
