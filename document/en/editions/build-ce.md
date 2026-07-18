# CE Build Pipeline
> Last updated: 2026-07-02

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

- **Backend EE modules**: SSO / team permissions (`core/auth/sso.py`, `team_permissions.py`, …), cloud storage (`core/storage/s3.py`, `oss.py`), persistent sandbox providers (the whole opensandbox / cube set), memory audit, skill distillation, the license verification implementation `core/licensing/_ee_verify.py`, EE services (team / sso_sync / distillation / sandbox_rebuild / security / cube_template_builder);
- **EE routes**: `api/routes/v1/admin_*.py`, `config_*.py`, `audit.py`, `auth.py`, `team_files.py`, `service_configs.py`, `data_sources.py`, `db_metadata.py`, `gateway_*.py`;
- **Industry MCP servers**: `mcp_servers/query_database_mcp/**`, `ai_chain_information_mcp/**`;
- **The entire main-repo alembic chain** (`alembic/versions/**` — CE uses an independent chain from the overlay, see below);
- **10 industry/branded skills** (under `skill_bundles/marketplace/`; the first 5 hard-depend on EE industry MCPs, the other 5 contain branded domain copy);
- **EE-coupled tests** and `tests/licensing/**`;
- **Frontend consoles**: `AdminApp.tsx`, `ConfigApp.tsx`, `components/admin/**`, `components/config/**` (lab = the automation lab is CE and stays);
- **Root-level EE deployment assets and open-source hygiene items**: EE Dockerfiles / compose fragments, LiteLLM gateway config (`docker/litellm/**`), `internal design docs`, `CLAUDE.md`, `.github/**`, internal `.env` defaults, branded manual PDFs, non-redistributable commercial fonts (`resources/fonts/**` — the overlay leaves a README placeholder to keep the Dockerfile COPY alive), a skill with embedded third-party credentials, the issuance tool `scripts/license_tool.py`, and the generator itself (`ce/**`, `scripts/build_ce.py`);
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
| `requirements` | `requirements.txt` | drops cloud storage / persistent-sandbox deps (boto3 / oss2 / opensandbox); moves neo4j / mem0ai into the optional `requirements-mem0.txt` |
| `docker_compose` | `docker-compose.yml` | removes the opensandbox / litellm services and their depends_on; un-profiles `script-runner` so it starts by default; strips env injections of excluded components tree-wide (`OPENSANDBOX_` / `CUBE_` / `S3_` / `OSS_` / `MODEL_GATEWAY_` / `LITELLM_` prefixes) |
| `frontend_lock` | `package-lock.json` + frontend Dockerfile | deletes the lock (inevitably out of sync with the pruned package.json) and rewrites `npm ci` to `npm install` |

### 4. `split` — assertion for files mixing user + admin endpoints

The three route files `content.py` / `models.py` / `projects.py` contain both user and admin endpoints; CE takes user-subset versions from the overlay. **build_ce.py asserts before the overlay step that these files exist in the overlay** — the main repo's full versions must never leak into CE; a missing file fails the build.

### 5. `overlay` — whole-file CE replacements / additions

`ce/overlay/` is layered on after transforms/prunes (contents must be self-clean; they are not transformed again). Current inventory and purposes:

| Overlay file | Purpose |
|---|---|
| `README.md` / `README_CN.md` / `LICENSE` / `NOTICE` / `CONTRIBUTING.md` / `SECURITY.md` | CE open-source repo front matter; English is the default README and Chinese remains available as a language alternative |
| `install.sh` | Public one-command installer for the personal no-Docker profile |
| `.env.example` | CE environment template (`JX_EDITION=ce`, no intranet IPs / brand defaults) |
| `resources/fonts/README.md` | Commercial-font placeholder (keeps the Dockerfile COPY path alive) |
| `src/backend/core/licensing/manager.py` | **CE stub**: `mode()` always `"ce"`, `has()` always False, unlimited seats, no verification logic at all |
| `src/backend/core/auth/permissions_iface.py` | Single-tenant permission-interface stub (seam C3): your own resources are always full-permission; team permission is always `none` (legacy team data migrated from EE must not become world-readable through a permissive stub) |
| `src/backend/core/memory/audit.py` | Memory-audit no-op stub (same interface, writes nothing) |
| `src/backend/alembic/versions/ce_0001_initial.py` | CE independent migration-chain baseline (next section) |
| `src/backend/api/routes/v1/{content,models,projects}.py` | User-subset versions of the split files |
| `src/backend/mcp_servers/_ports.py` | Port table for the 8 general tools (EE industry-tool ports marked reserved) |
| `src/frontend/default.conf.template` | CE frontend Nginx template with `/gateway/**` proxying and the litellm upstream removed |
| `src/frontend/src/main.tsx` | CE entry: mounts only the main app / API docs / share preview — no /admin, no /config |
| `src/frontend/src/updates.ts` | CE release-notes data |
| `.claude/skills/hugagent-{backend,frontend}-dev/…` | CE versions of the project dev skills' SKILL.md and references (admin-console / EE router-registration sections stripped) |

> The router registry `api/routes/v1/__init__.py` needs **no** overlay copy: `iter_edition_routers` silently skips EE modules that are physically absent, so the same file is shared by both trees.

### 6. `brand_scan` — the brand-gate regex file

`ce/brand_scan.txt` is one regex per line (case-insensitive); any hit fails the build. Currently intercepts: upstream brand tokens (several Chinese and English forms), internal field names, intranet IP ranges and test-machine IPs, personal email domains (@163 / @126 / @qq), and third-party API-key shapes (`ak_[0-9a-f]{16,}`).

## build_ce.py step pipeline

```
[1/7] Copy      git ls-files (cached + untracked-unignored) as the whitelist, minus exclude
                and default ignores
[1/7] Rename    apply optional path migrations from manifest.renames (currently empty)
[2/7] Transform manifest.transforms tree-wide text rewrites (binaries skipped; source-code
                literals from other product lines produce a warning)
[3/7] Prune     the five pruners in manifest.prunes
[4/7] Overlay   first assert the split files exist in the overlay, then layer the whole tree
                (skipping __pycache__/pyc)
[5/7] Brand gate line-by-line text regex must hit zero + a full file-PATH scan (covers binary
                asset filenames; an extra path-only pattern blocks commercial font files);
                the count of unscannable binaries is reported with the result
[6/7] LICENSE gate refuse to generate while the overlay LICENSE is still placeholder text
                (contains the NOTE TO MAINTAINERS marker)
[7/7] Self-checks --import-check / --pytest-check / --frontend-check (optional)
[8/8] Cleanup   residues left by the self-checks: __pycache__ / .pytest_cache / node_modules /
                dist / regenerated lock
```

Using `git ls-files` as the copy list means `.env`, local databases, and other untracked/ignored files **can never enter the CE tree**.

## CE database differences

CE does not create EE-only tables; the single source of truth is `src/backend/core/db/edition_tables.py`:

- `EE_ONLY_TABLES` (18 tables): `teams`, `team_members`, `team_folders`, `invite_codes`, `roles`, `role_assignments`, `kb_grants`, `audit_logs`, `memory_audit`, `model_pricing`, `data_sources`, `ds_table_meta`, `ds_column_meta`, `ds_golden_sql`, `gateway_virtual_keys`, `sandbox_rebuilds`, `admin_skill_drafts`, `distillation_runs`.
- `ce_create_all(bind)`: creates tables on a **cloned MetaData** after filtering — CE tables carry cross-boundary FK constraints into EE tables (e.g. `projects → teams`; design decision D3 "keep the column, always NULL"); shipped as-is they would fail on PostgreSQL because the referenced tables do not exist, so the constraints are stripped on the clone (columns kept) while the original metadata and ORM mappings stay untouched. Every name in the set must actually exist in metadata (asserted in the function) so a renamed model cannot silently degrade the filter into a full create_all.

Two table-creation entry points share this filter:

1. The CE branch of `core/db/engine.py::init_db` (when `JX_EDITION=ce`, startup fallback uses `ce_create_all`);
2. The CE overlay migration baseline `ce_0001_initial.py` — CE runs an **independent alembic chain** (it does not replay the main repo's 50+ historical migrations); the baseline is "create_all filtered by `EE_ONLY_TABLES`", dialect-aware (SQLite and PostgreSQL), same source and same filter as init_db, both idempotent and non-conflicting. Subsequent CE schema evolution adds regular migrations on the `ce_0001` chain.

EE (including internal / licensed and every other license state) always creates the full schema, identical to historical behavior. Maintenance rule: **when adding an EE-only model, add its table name to `EE_ONLY_TABLES`**.

## Release acceptance

A qualifying release build must pass all of:

| Gate | Criterion | Enforced by |
|---|---|---|
| Zero EE route leakage | the CE tree physically lacks EE routes/modules; `import api.app` succeeds under `--import-check` (missing modules skipped by the registry); `--pytest-check` collection reports no EE import errors | exclude + `iter_edition_routers` |
| Brand gate | zero text hits; full path scan passes; new binary assets (content-scan blind spot) require manual review | `brand_scan()` |
| LICENSE gate | the overlay LICENSE is not placeholder text | `license_placeholder_check()` |
| Split assertion | the CE subsets of the three split files exist in the overlay | pre-overlay check in `main()` |
| Frontend buildability | `--frontend-check`: npm install + vite build succeed | `frontend_check()` |
| Delivery hygiene | all self-check residue removed | `cleanup_gate_artifacts()` |

## Day-to-day maintenance

- **New EE route**: register in `EE_ROUTERS` (see [Backend Development Guide](../development/backend.md)) + add the file glob to `manifest.exclude` (`admin_*.py` / `config_*.py` are already covered by wildcards).
- **New EE table**: add the name to `EE_ONLY_TABLES`.
- **New EE dependency / compose service**: add a drop entry to the relevant prune section.
- **New branded asset**: confirm brand_scan can intercept it at the path or text level; binaries are a content-scan blind spot and rely on path patterns + manual review.
- After changes, run `python scripts/build_ce.py --allow-dirty --import-check --pytest-check` to validate.

## Related source

| Topic | Path |
|---|---|
| Derivation manifest (the only input) | `ce/manifest.yaml` |
| Generator | `scripts/build_ce.py` |
| Brand-gate patterns | `ce/brand_scan.txt` |
| Overlay directory | `ce/overlay/` |
| CE/EE table boundary | `src/backend/core/db/edition_tables.py` |
| Startup table-creation CE branch | `src/backend/core/db/engine.py::init_db` |
| CE migration baseline | `ce/overlay/src/backend/alembic/versions/ce_0001_initial.py` |
| Router registry | `src/backend/api/routes/v1/__init__.py` |

See also: [Community vs. Enterprise Edition](overview.md) · [License Mechanism](license.md)
