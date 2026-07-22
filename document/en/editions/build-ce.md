# CE Build Pipeline
> Last updated: 2026-07-22

The Community Edition (CE) is not a separate branch. It is a subset tree **deterministically derived** from the main repo (EE, the single development source of truth) by `scripts/build_ce.py`, written to `dist/ce/`. The core constraint is the **whitelist iron rule: EE-only code is physically absent from the CE tree** — not commented out, not flag-disabled, but removed at the file level. The pipeline's only input is `ce/manifest.yaml`.

## Quick start

```bash
pip install pyyaml                                  # the generator's only third-party dependency
python scripts/build_ce.py                          # generate + brand gate + LICENSE gate
python scripts/build_ce.py --import-check           # + import api.app self-check inside the CE tree
python scripts/build_ce.py --pytest-check           # + pytest --collect-only self-check
python scripts/build_ce.py --frontend-check         # + npm install && build self-check (needs network)
python scripts/build_ce.py --allow-dirty            # dev only: generate with uncommitted changes
python scripts/build_ce.py --allow-placeholder-license  # dev only: pass with a placeholder LICENSE
```

Release builds require a clean working tree (built from committed state); output defaults to `dist/ce/` (`--out` to change).

## Manifest structure (`ce/manifest.yaml`)

The manifest has the following sections, in processing order:

### 1. `exclude` — physical removal of EE-only assets

Glob patterns (relative to the repo root); a match means the file is never copied. Covers:

- **Backend EE modules**: the complete `edition_ee/**` implementation root (Team/RBAC, SSO, license verification and gate, EE ORM, Dify integration), cloud storage (`core/storage/s3.py`, `oss.py`), persistent sandbox providers, memory audit, skill distillation, and other EE services;
- **EE routes**: `api/routes/v1/admin_*.py`, `config_*.py`, `audit.py`, `auth.py`, `team_files.py`, `service_configs.py`, `data_sources.py`, `db_metadata.py`, `gateway_*.py`;
- **Industry MCP servers**: `mcp_servers/query_database_mcp/**`, `ai_chain_information_mcp/**`;
- **The entire main-repo alembic chain** (`alembic/versions/**` — CE uses an independent chain from the overlay, see below);
- **10 industry/branded skills** (under `skill_bundles/marketplace/`; the first 5 hard-depend on EE industry MCPs, the other 5 contain branded domain copy);
- **EE-coupled tests** and `tests/licensing/**`;
- **Frontend consoles**: `AdminApp.tsx`, `ConfigApp.tsx`, `components/admin/**`, `components/config/**` (lab = the automation lab is CE and stays);
- **Root-level EE deployment assets and open-source hygiene items**: EE Dockerfiles / compose fragments, LiteLLM gateway config (`docker/litellm/**`), `internal design docs`, `CLAUDE.md`, `.github/**`, internal `.env` defaults, branded manual PDFs, bundled commercial-font assets, a skill with embedded third-party credentials, the issuance tool `scripts/license_tool.py`, and the generator itself (`ce/**`, `scripts/build_ce.py`);
- **EE-only templates inside the project dev skills**: `.claude/skills/*/templates/admin_route.py`, `admin-editor.tsx` (admin routes / the admin console physically don't exist in CE).

### 1.5 `renames` — optional path renames

Transforms rewrite file contents only, never paths, so this step remains available for paths that genuinely need migration. CE and EE now share the HugAgentOS brand and `hugagent` technical identifiers; `renames` is empty and paths such as `.claude/skills/hugagent-*-dev` remain unchanged.

### 2. `transforms` — brand-consistency and open-source hygiene rewrites

Applied tree-wide to text files in declaration order. The CE product name is `HugAgentOS` (the `product_name` field), while technical identifiers used by containers, environment variables, and the CLI remain `hugagent`. HugAgentOS literals from another product line are normalized to HugAgentOS, and the historical display name `HugAgentOS` is upgraded with a negative-lookahead regex so `OS` is never appended twice.

> The generator separately calls out any `src/**` source file that still contains another product line's brand literals (the `_GENERIC_BRAND_TOKENS` check). Such hard-coding should move to `settings.branding` / DB seeds instead of relying on derivation rewrites indefinitely.

### 3. `prunes` — structural surgery

Content edits that plain text substitution cannot express, implemented in `build_ce.py`'s `PRUNERS` table:

| Pruner | Target | Action |
|---|---|---|
| `catalog_json` | `core/config/catalog.json` | drops the EE MCP seeds (`database_query`, `query_database`, `ai_chain_information_mcp`) |
| `package_json` | `src/frontend/package.json` | renames to `hugagent-ui`; drops the commercially licensed `@univerjs/preset-sheets-advanced` and dead dependency `pptxgenjs`; pins `@univerjs/icons=1.1.1` to preserve the Univer 0.19 export contract when the main-repo lockfile is absent |
| `requirements` | `requirements.txt` | drops cloud storage / persistent-sandbox deps (boto3 / oss2 / opensandbox); moves neo4j / mem0ai into the separate `requirements-mem0.txt` (installed by default by the no-Docker installer) |
| `docker_compose` | `docker-compose.yml` | removes the opensandbox / litellm services and their depends_on; un-profiles `script-runner` so it starts by default; strips env injections of excluded components tree-wide (`OPENSANDBOX_` / `CUBE_` / `S3_` / `OSS_` / `MODEL_GATEWAY_` / `LITELLM_` prefixes) |
| `frontend_lock` | `package-lock.json` + frontend Dockerfile | deletes the lock (inevitably out of sync with the pruned package.json) and rewrites `npm ci` to `npm install` |
| `repository_resources` | build and source references to bundled commercial fonts | removes the font-copy/install stanzas from CE Dockerfiles and the backend fallback to the repository font directory; a forbidden-artifact gate verifies the generated tree again |

### 4. `split` — assertion for files mixing user + admin endpoints

`manifest.split` explicitly lists every edition seam that CE must replace as a whole file. **build_ce.py asserts each replacement exists before applying the overlay** — a full source-tree implementation must never leak into CE; a missing replacement fails the build.

### 5. `overlay` — whole-file CE replacements / additions

`ce/overlay/` is layered on after transforms/prunes (contents must be self-clean; they are not transformed again). Current inventory and purposes:

| Overlay file | Purpose |
|---|---|
| `README.md` / `README_CN.md` / `LICENSE` / `NOTICE` / `CONTRIBUTING.md` / `SECURITY.md` | CE open-source repo front matter; English is the default README and Chinese remains available as a language alternative |
| `install.sh` | Public one-command installer for the personal no-Docker profile |
| `.env.example` | CE environment template (`JX_EDITION=ce`, no intranet IPs / brand defaults) |
| `.hugagent-edition` | Machine-readable `ce` marker used only after derivation; it lets release tooling distinguish a derived CE checkout from a source checkout whose generator is unexpectedly missing |
| `.github/workflows/desktop-release.yml` | Public CE desktop release workflow, including the release-tag/version preflight gate |
| `src/backend/api/routes/v1/__init__.py` | CE route registry; `EE_ROUTERS` is always empty |
| `src/backend/core/auth/permissions_iface.py` | Owner-only single-tenant authorization interface with no team permission exports |
| `src/backend/core/services/artifact_edition.py` | Personal artifact-scope interface with no team fields, permissions, or repository methods |
| `src/backend/core/llm/tools/edition_{myspace,myspace_vfs,artifact_recovery}.py` | Personal MySpace tool, VFS, and recovery interfaces; organization implementations do not enter CE |
| `src/backend/core/config/edition_display_names.py` | CE tool display names with no team-tool names |
| `src/backend/core/memory/audit.py` | Memory-audit no-op stub (same interface, writes nothing) |
| `src/backend/alembic/versions/ce_0001_initial.py` | CE independent migration-chain baseline (next section) |
| `src/backend/api/routes/v1/{agents,content,kb_models,projects}.py` | CE API contracts with administration endpoints and organization fields removed |
| `src/backend/mcp_servers/_ports.py` | Port table for the 8 general tools (EE industry-tool ports marked reserved) |
| `src/frontend/default.conf.template` | CE frontend Nginx template with `/gateway/**` proxying and the litellm upstream removed |
| `src/frontend/src/main.tsx` | CE entry: mounts only the main app / API docs / share preview — no /admin, no /config |
| `src/frontend/src/updates.ts` | CE release-notes data |
| `.claude/skills/hugagent-{backend,frontend}-dev/…` | CE versions of the project dev skills' SKILL.md and references (admin-console / EE router-registration sections stripped) |

> License, Team/RBAC, EE ORM, and Dify implementations all live under `edition_ee/**`. CE supplies no same-name implementation stubs; module discovery for `edition_ee` must report that it is absent.

The team-file repository, organization MySpace tools, VFS, and artifact recovery
implementations live in `edition_ee/db/artifact_repository.py` and
`edition_ee/services/{myspace_tools,myspace_vfs,artifact_recovery}.py`.
Shared modules expose only edition-neutral call interfaces. CE overlays implement the
personal profile without retaining commercial fields or tool names.

### 6. `brand_scan` — the brand-gate regex file

`ce/brand_scan.txt` is one regex per line (case-insensitive); any hit fails the build. Currently intercepts: upstream brand tokens (several Chinese and English forms), internal field names, intranet IP ranges and test-machine IPs, personal email domains (@163 / @126 / @qq), and third-party API-key shapes (`ak_[0-9a-f]{16,}`).

## build_ce.py step pipeline

```
[1/7] Copy      git ls-files --cached as the whitelist, minus exclude
                and default ignores
[1/7] Rename    apply optional path migrations from manifest.renames (currently empty)
[2/7] Transform manifest.transforms tree-wide text rewrites (binaries skipped; source-code
                literals from other product lines produce a warning)
[3/7] Prune     the five pruners in manifest.prunes
[4/7] Overlay   first assert the split files exist in the overlay, then layer the whole tree
                (skipping __pycache__/pyc)
[4/7] Forbidden assert zero EE paths, table names, foreign keys, and commercial runtime-source
                symbols; test directories may retain only the negative contract assertions
[4/7] Binary gate every PNG/PDF/DOCX must match a manually/OCR-reviewed path + SHA-256 allowlist
[5/7] Brand gate line-by-line text regex must hit zero + a full file-PATH scan (covers binary
                asset filenames; an extra path-only pattern blocks commercial font files)
[6/7] LICENSE gate refuse to generate while the overlay LICENSE is still placeholder text
                (contains the NOTE TO MAINTAINERS marker)
[7/7] Self-checks --import-check / --pytest-check / --frontend-check (optional)
[8/8] Cleanup   residues left by the self-checks: __pycache__ / .pytest_cache / node_modules /
                dist / regenerated lock
```

Using `git ls-files` as the copy list means `.env`, local databases, and other untracked/ignored files **can never enter the CE tree**.

The Windows desktop payload follows the same boundary in both checkout types. In the source checkout, `desktop/scripts/prepare-bundle.mjs` finds and runs `scripts/build_ce.py`. In a derived CE checkout, where the generator is intentionally absent, it requires `.hugagent-edition` to contain `ce` and stages only the current checkout's tracked files. Release builds reject a dirty checkout. This fallback cannot silently turn an arbitrary repository into a CE payload.

## CE database differences

CE does not register or create EE-only tables. Enterprise ORM classes live under `src/backend/edition_ee/db/models/`, and that package is physically absent from the derived tree. The CE model export contains only CE mappings; compatibility attributes come from CE model extensions and do not register commercial columns or tables.

The release contract checks 20 forbidden table names: `chat_session_user_states`, `teams`, `team_members`, `team_folders`, `invite_codes`, `roles`, `role_assignments`, `kb_grants`, `marketplace_visibility_grants`, `audit_logs`, `memory_audit`, `model_pricing`, `data_sources`, `ds_table_meta`, `ds_column_meta`, `ds_golden_sql`, `gateway_virtual_keys`, `sandbox_rebuilds`, `admin_skill_drafts`, and `distillation_runs`. The import gate fails if any is registered in CE metadata or referenced by a CE foreign key. It also rejects the corresponding commercial-scope columns on `projects`, `artifacts`, `user_agents`, `chat_sessions`, `marketplace_listing_states`, and `sites`.

Two table-creation entry points use the CE-only metadata:

1. The CE branch of `core/db/engine.py::init_db` (when `JX_EDITION=ce`, startup fallback uses `ce_create_all`);
2. The CE overlay migration baseline `ce_0001_initial.py` — CE runs an **independent alembic chain** and creates directly from CE-only metadata, dialect-aware (SQLite and PostgreSQL), idempotent with `init_db`. Subsequent CE schema evolution adds regular migrations on the `ce_0001` chain.

EE always creates the full schema. Maintenance rule: **new EE mappings belong under `edition_ee/db/models`, and their table names must be added to the CE release contract**.

## Release acceptance

A qualifying release build must pass all of:

| Gate | Criterion | Enforced by |
|---|---|---|
| Zero EE route/schema leakage | the CE tree physically lacks `edition_ee`; `EE_ROUTERS` is empty; organization routes, fields, and wording are absent from OpenAPI; forbidden tables, foreign keys, and commercial-scope columns are absent from metadata | `--import-check` |
| Zero commercial runtime symbols | backend and frontend runtime sources contain no Team/RBAC model, scope-field, permission, or tool symbols; tests retain only negative assertions | `find_forbidden_artifacts()` + CE runtime contract |
| Brand / binary gate | zero text hits and a clean full-path scan; every PNG/PDF/DOCX matches a manually/OCR-reviewed path + SHA-256 allowlist | `brand_scan()` + `binary_allowlist_check()` |
| LICENSE gate | the overlay LICENSE is not placeholder text | `license_placeholder_check()` |
| Split assertion | every declared CE split replacement exists in the overlay | pre-overlay check in `main()` |
| Frontend buildability | `--frontend-check`: npm install + vite build succeed | `frontend_check()` |
| Delivery hygiene | all self-check residue removed | `cleanup_gate_artifacts()` |

## Day-to-day maintenance

- **New EE route**: register in `EE_ROUTERS` (see [Backend Development Guide](../development/backend.md)) + add the file glob to `manifest.exclude` (`admin_*.py` / `config_*.py` are already covered by wildcards).
- **New EE table**: define it under `edition_ee/db/models` and add the name to `contracts.forbidden_tables`.
- **New EE dependency / compose service**: add a drop entry to the relevant prune section.
- **New PNG/PDF/DOCX**: manually inspect or OCR-review its content, then add its relative path and SHA-256 to `ce/binary_allowlist.sha256`; any hash change requires a fresh review.
- After changes, run `python scripts/build_ce.py --allow-dirty --import-check --pytest-check --frontend-check`. Release builds must not use `--allow-dirty`, and untracked files are never copy inputs.

## Related source

| Topic | Path |
|---|---|
| Derivation manifest (the only input) | `ce/manifest.yaml` |
| Generator | `scripts/build_ce.py` |
| Brand-gate patterns | `ce/brand_scan.txt` |
| Overlay directory | `ce/overlay/` |
| Derived-tree edition marker | `ce/overlay/.hugagent-edition` |
| Public desktop release workflow | `ce/overlay/.github/workflows/desktop-release.yml` |
| CE/EE table boundary | `src/backend/core/db/edition_tables.py` |
| Startup table-creation CE branch | `src/backend/core/db/engine.py::init_db` |
| CE migration baseline | `ce/overlay/src/backend/alembic/versions/ce_0001_initial.py` |
| Router registry | `src/backend/api/routes/v1/__init__.py` |

See also: [Community vs. Enterprise Edition](overview.md) · [License Mechanism](license.md)
