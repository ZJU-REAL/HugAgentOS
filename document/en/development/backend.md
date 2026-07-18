# Backend Development Guide
> Last updated: 2026-06-11

The backend is FastAPI + SQLAlchemy + AgentScope 2.0, located in `src/backend/`. This page covers the development model, common commands, layering conventions, route registration, and the steps for adding MCP tools and skills. For the big picture, read [Architecture Overview](../architecture/overview.md) and [Backend Architecture](../architecture/backend.md) first.

## Development model: everything runs in Docker

There is no long-running local dev server — **backend code changes only take effect after rebuilding the image and restarting the container**:

```bash
# Backend changed
docker-compose up -d --build backend

# Both backend and frontend changed
docker-compose up -d --build backend frontend

# Dependency / Dockerfile changes: force a clean rebuild
docker-compose build --no-cache backend
docker-compose up -d backend

# Logs
docker-compose logs -f backend
```

> When new code "doesn't take effect", 90% of the time a cached layer was hit — verify the file inside the container at `/app/src/backend/...` actually changed, and rebuild with `--no-cache` if necessary.
>
> The Makefile offers `make dev` (local uvicorn with `--reload` on port 3001) for quick bare-API debugging, but the full chain (nginx / mcp / database / sandbox) depends on the compose stack; verify against Docker.

## Make targets

The following targets are taken from the root `Makefile`:

| Target | What it runs |
|---|---|
| `make test` | `PYTHONPATH=src/backend pytest src/backend/tests/ -v --cov=src/backend` (with coverage reports) |
| `make selftest` | same, but `-x -q` (fail fast) |
| `make format` | `black . --line-length=100` + `isort . --profile black` |
| `make lint` | the `--check` mode of the above (check only, no rewrites) |
| `make type-check` | `mypy src/backend --ignore-missing-imports` |
| `make security-scan` | bandit + safety |
| `make migrate` | `alembic upgrade head` |
| `make migrate-new msg="..."` | `alembic revision --autogenerate -m "..."` |
| `make migrate-down` / `migrate-history` | roll back one step / show history |
| `make db-reset` / `db-seed` | reset (destructive) / seed sample data |
| `make build` / `up` / `down` / `logs` / `ps` etc. | docker-compose wrappers |

> ⚠️ The committed tree is not formatter-clean: **do not run black/isort over files you did not touch** (it buries the real diff in formatting noise); format only the scope you changed.

### Running a single test file

```bash
PYTHONPATH=src/backend pytest src/backend/tests/test_foo.py -v
PYTHONPATH=src/backend pytest src/backend/tests/api/test_bar.py::test_case -v
```

Tests can run inside the backend container or in a local venv with `PYTHONPATH` set. Test files are named `test_*.py` and live in the matching subdirectory of `src/backend/tests/`.

## Alembic migration flow

1. Change the ORM models in `src/backend/core/db/models.py`;
2. `make migrate-new msg="add xxx table"` to autogenerate the migration (**review the output by hand** — autogenerate cannot be fully trusted);
3. Apply with `make migrate` (or `alembic upgrade head` inside the container);
4. If the table is **EE-only**: add its name to `core/db/edition_tables.py::EE_ONLY_TABLES` (CE does not create empty EE tables; names in the set are existence-asserted).

Note: the main-repo alembic chain is the EE chain; the CE derived tree uses its own `ce_0001` baseline chain (see [CE Build Pipeline](../editions/build-ce.md#ce-database-differences)) — new main-repo migrations never enter CE.

## Coding conventions

### Layered architecture

```
api/routes/v1/*.py    route layer: validation, dependency injection, ORM→dict mapping, envelope wrapping
core/services/*.py    service layer: business logic, permission checks
core/db/repository.py repository layer: CRUD, soft-delete filtering (deleted_at IS NULL)
core/db/models.py     ORM models
```

**No cross-layer calls** (a route reaching directly into ORM queries is a violation). A Service takes a `Session` in its constructor and creates its Repository internally.

### Unified response envelope

All v1 endpoints return `{ code, message, data, trace_id, timestamp }`; always use the helpers from `core/infra/responses.py`:

```python
from core.infra.responses import success_response, created_response, paginated_response

return success_response(data={"id": item.id})
return created_response(data=_item_to_dict(item))          # POST creation with 201
return paginated_response(items=[...], page=page, page_size=page_size, total_items=total)
```

### Error handling

**Never hand-roll error responses / HTTPException in routes** — raise the exceptions from `core/infra/exceptions.py` and let the global error handler render the envelope:

```python
from core.infra.exceptions import BadRequestError, ResourceNotFoundError

raise ResourceNotFoundError("chat_session", chat_id)
raise BadRequestError("parameter 'name' must not be empty")
```

The same applies to license 402s: the single source is `FeatureNotLicensed` (40201) / `SeatLimitExceeded` (40202) in `core/licensing/features.py`; routes and services must not hand-craft 402s. Error-code ranges are documented in [Error Codes](../api/error-codes.md).

### Dependency injection

```python
from api.deps import get_current_user, get_db, require_admin, require_config

user: UserContext = Depends(get_current_user)   # regular endpoints
_: None = Depends(require_admin)                # /admin content-console endpoints (ADMIN_TOKEN)
_: None = Depends(require_config)               # /config system-console endpoints (CONFIG_TOKEN)
db: Session = Depends(get_db)
```

## Registering a new route (the CE/EE registry)

Routes are **no longer** included line by line in `api/app.py`. The single source of truth is the pair of registries in `src/backend/api/routes/v1/__init__.py`; `app.py` registers from the tables in a loop (CE first, then EE):

```python
# api/routes/v1/__init__.py
CE_ROUTERS: tuple[tuple[str, str], ...] = (
    ("chats", "router"),
    ...
    ("meta", "router"),
)

EE_ROUTERS: tuple[tuple[str, str, str | None], ...] = (
    ("audit", "router", "audit"),              # third column = license feature bit
    ("admin_skills", "router", "content_admin"),
    ("config_verify", "router", None),         # None = explicit exemption from the feature guard
    ...
)
```

Steps for a new route:

1. Create the route file under `api/routes/v1/` with `router = APIRouter(prefix="/v1/xxx", tags=["Xxx"])`;
2. Decide the edition:
   - **CE capability** (self-contained for an individual) → append `("module_name", "router")` to `CE_ROUTERS`;
   - **EE capability** (organization-scale) → append `("module_name", "router", "<feature>")` to `EE_ROUTERS`, with the feature taken from `core/licensing/features.py::Feature`; use `None` only when the endpoint must remain reachable with an invalid license (login / license-swap infrastructure), and document why;
3. For EE routes, also exclude the file in `ce/manifest.yaml` (`admin_*.py` / `config_*.py` are already covered by wildcard patterns);
4. Table order is registration order; relative order within a prefix family is invariant (e.g. the public-read `config` must precede the `config_*` console routes).

`iter_edition_routers` silently skips modules that are physically absent (the CE tree), so this file ships into CE unchanged with no overlay copy. EE entries' feature bits are turned by `app.py` into `requires_feature(Feature(...))` router-level dependencies; unauthorized access returns 402 (see [License Mechanism](../editions/license.md)).

## Adding an MCP server

Each MCP tool is a long-running streamable-http process inside the `mcp` container; the backend connects via `HttpStatefulClient`:

1. Create `src/backend/mcp_servers/<name>_mcp/` (model it on `internet_search_mcp/`: `server.py` + `_selftest.py`);
2. Allocate a port in `src/backend/mcp_servers/_ports.py::PORTS` (port assignments are a stable contract — never reuse or reshuffle); `core/config/mcp_config.py::MCP_SERVERS` derives the connection config from it automatically;
3. Add display names and descriptions in `src/backend/core/config/display_names.py`;
4. Add a seed entry to the `mcp` array in `src/backend/core/config/catalog.json` (`id` / `kind: "mcp_server"` / `name` / `desc` / `enabled` / `config.server`) — the catalog is the single source of truth for capability toggles;
5. Local debugging:

```bash
PYTHONPATH=src/backend python -m mcp_servers.<name>_mcp.server
PYTHONPATH=src/backend python -m mcp_servers.<name>_mcp._selftest
```

6. Rebuild the mcp container (`docker-compose up -d --build mcp`);
7. If the tool is an **EE industry tool**: exclude the whole directory in `ce/manifest.yaml`, add its id to `prunes.catalog_json.drop_mcp_ids`, and mark its port reserved in the CE overlay's `_ports.py`.

## Adding a skill (Agent Skill)

Skill loading is multi-source (`core/agent_skills/config.py`): built-in (`skill_bundles/default/`, always-on), admin (DB / `/app/storage/admin_skills/`), user, and project sources merge by priority. `skill_bundles/marketplace/` holds install-based marketplace seeds scanned separately by the marketplace service — it is **not** part of the default loading sources.

Adding a **built-in skill**:

1. Create `src/backend/skill_bundles/default/<skill-id>/SKILL.md` (id rule: lowercase letters / digits / `-_`, ≤63 chars);
2. Write the SKILL.md frontmatter (`name` / `description` / optional `version` / `tags` / `allowed_tools`) plus the instruction body;
3. Optionally add `scripts/` (executable scripts; `.py/.js/.sh/.r` are auto-whitelisted when `_scripts.json` is absent), `references/`, `evals/`;
4. Rebuild the backend container.

A **marketplace skill** goes under `skill_bundles/marketplace/<skill-id>/` with the same structure plus marketplace metadata (model on an existing entry); skills with brand or industry dependencies must also be added to the `ce/manifest.yaml` exclusion list. At runtime a skill's directory is presented inside the sandbox as `/workspace/skills/<id>`; the `{dir}` placeholder in prompts resolves to that path.

## New-feature checklist

- [ ] ORM model + indexes + timestamps (`core/db/models.py`); EE tables added to `EE_ONLY_TABLES`
- [ ] Repository filters soft deletes; Service carries business logic and permission checks
- [ ] Routes use envelope responses; errors go through `core/infra/exceptions`
- [ ] Route registered in `CE_ROUTERS` / `EE_ROUTERS` (EE with a feature bit + manifest exclusion)
- [ ] Alembic migration generated and reviewed
- [ ] Tests written; `make selftest` passes
- [ ] Format / lint only the changed scope

## Related source

| Topic | Path |
|---|---|
| FastAPI entry / registration loops | `src/backend/api/app.py` |
| Router registry | `src/backend/api/routes/v1/__init__.py` |
| Response envelope | `src/backend/core/infra/responses.py` |
| Exception hierarchy | `src/backend/core/infra/exceptions.py` |
| Dependency injection | `src/backend/api/deps.py` |
| ORM / table boundary | `src/backend/core/db/models.py`, `src/backend/core/db/edition_tables.py` |
| MCP port table / connection config | `src/backend/mcp_servers/_ports.py`, `src/backend/core/config/mcp_config.py` |
| Capability catalog | `src/backend/core/config/catalog.json` |
| Skill loading | `src/backend/core/agent_skills/` (`config.py` / `loader.py` / `registry.py`) |
| Workflow orchestration | `src/backend/orchestration/workflow.py` |

See also: [Frontend Development Guide](frontend.md) · [API Overview](../api/overview.md) · [MCP Tools](../modules/mcp-tools.md) · [Agent Skills](../modules/agent-skills.md)
