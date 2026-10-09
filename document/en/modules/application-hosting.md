# Application databases and hosted MCP

Static sites do not create databases. Persistent forms create typed application tables; MCP is published on request and can exist without a site. Application data uses a separate PostgreSQL database, not the platform database.

## Deployment

Set `APPLICATION_DATABASE_URL` to a separate database and run `python -m core.services.application_hosting_setup` once. Cloud installations without this setting show hosting as unavailable; local mode may use SQLite. Production uses PostgreSQL.

The optional `docker-compose.application-hosting.yml` adds PostgreSQL, a persistent volume and an initialization job. Set different URL-safe `APPLICATION_DB_PASSWORD` (bootstrap) and `APPLICATION_OWNER_DB_PASSWORD` (runtime), or encode connection strings correctly. The initialization job uses the same Dockerfile build target as the backend and mounts current backend sources read-only. Build the backend from these sources and rebuild the frontend for the nginx `/applications-mcp/` route. Follow repository approval rules before deployment. End-to-end tests use separate databases and platform mock authentication for local testing only.

```bash
docker compose -f docker-compose.yml -f docker-compose.application-hosting.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.application-hosting.yml up -d
```

## Access and data

Site management has a Data and MCP tab for linked applications. The Sites page uses one shared card design for sites, MCP services with defined tools, and databases without an active linked site. Shared search covers site titles and addresses, plus MCP titles, tool names and descriptions. Databases linked to an active site remain in site management.
MCP cards provide Open, Copy link, Edit and Manage. Open shows the connection URL, status and tools instead of navigating to a protocol endpoint. Edit opens the MCP source project conversation. Manage retains the complete tool form for adding/removing tools and changing names, descriptions, tables, returned fields and filters. Management pins the selected application and provides Data and MCP tabs. Publication shows the new credential once; revocation requires confirmation.
Deleting a webpage does not delete its application data or revoke MCP. Its retained database or MCP card still provides data inspection and access revocation. Tables have typed columns, constraints and indexes. The UI supports JSON import, paginated reads and full-table streaming CSV export. The REST update endpoint checks record versions; the UI does not yet include a row editor.

Browser management uses `/api/v1/applications` (backend `/v1/applications`). Public forms submit to `/site/{slug}/__api/data/{table}`. Only explicitly enabled `public_insert` tables accept visitor writes; site visibility and password checks apply, and visitor reads are disabled by default. Shared collections must explicitly enable public_read; whole-list replacement also requires public_replace and the current revision. Anonymous means an unauthenticated visitor, not anonymized data.

MCP uses `/applications-mcp/{application_id}`, Streamable HTTP and `Authorization: Bearer <credential>`. A credential is returned once and only its hash is stored. Republishing, revocation and configuration rollback invalidate previous credentials. Published tools expose only configured fields and filters through the same service as REST. Persistent configurations and data survive chat and sandbox lifetimes.

Each PostgreSQL application has a separate schema and restricted NOLOGIN role. Reads and writes switch roles, denying access to other applications and registry tables. Provisioning still uses a trusted administrative connection. Arbitrary SQL and database credentials are not exposed to generated pages.

## Industry data

The `manage_application` action `import_industry` requires enterprise edition and an installed industry plugin. It imports a snapshot from the existing enterprise MCP source, preserving identifiers and source information. Invalid or unavailable sources fail explicitly; there is no fabricated fallback. Updates are not automatically synchronized.

## Retired storage

Legacy KV and form collection models, routes, tools and UI are removed. Old `__api/kv` and `__api/forms` requests return 404 or 405. Update pages that still use these endpoints to the SQL data API before upgrading.

Platform migration `site05retire` archives all original rows and verifies SHA-256 before dropping `site_kv` and `site_submissions`. Backups use `STORAGE_PATH/migration-backups` or `SITE_STORAGE_BACKUP_DIR`. Files have mode 0600 and contain original business data. A failed archive prevents table removal.

To restore, set `SITE_STORAGE_RESTORE_FILE` to the verified archive path and follow the deployment rollback process. Downgrade restores fields, timestamps, JSONB and foreign keys. Restoring tables does not restore removed APIs; also restore the matching code version. Historical Alembic revisions remain part of the upgrade chain.

## Maintenance structure

Schema validation, relational table construction, registry transactions, data operations, MCP deployment and protocol adapters are separate modules. REST, agent tools and MCP share the data service. The UI separates API access and loading from the panel, cancels stale reads and checks that asynchronous publication results belong to the selected application.

## Limits

Implemented: typed SQL, bounded reads/writes, atomic batches, idempotency, optimistic updates, application isolation, anonymous insert-only forms and declarative MCP hosting. Not implemented: arbitrary-code MCP containers, OAuth authorization server, foreign-key relationship designer, backup/restore console and comprehensive usage billing. The real chat driver is `src/backend/tests/application_hosting_e2e.py`; `application_hosting_e2e_setup.py` prepares its isolated environment. It exercises the production chat API, model, tools, sandbox and publishing. Acceptance status comes from the actual run report.

## Reproduce local end-to-end acceptance

Use a local Docker environment with a real model, industry source and OpenSandbox configured. Build the current frontend first. These drivers use production APIs and actual tools, with no file or publication adapters. Test services bind loopback ports and use separate platform_e2e_verified / applications_e2e_verified databases and storage.

```bash
npm --prefix src/frontend run build
PYTHONPATH=src/backend .venv/bin/python src/backend/tests/application_hosting_e2e_setup.py
PYTHONPATH=src/backend .venv/bin/python src/backend/tests/application_hosting_e2e.py
```

Submit test data at `http://127.0.0.1:18533/site/e2e-form-1007/` using email `browser-e2e@example.test`. Then run:

```bash
PYTHONPATH=src/backend .venv/bin/python src/backend/tests/application_hosting_lifecycle_e2e.py
```

The lifecycle driver restarts only the isolated test services and rotates, rolls back, revokes and republishes the test MCP credential. Credentials stay in `/tmp/application-hosting-full-e2e-credentials.json` (0600), outside pages and acceptance reports. Existing batches are not automatically reset; use a clean isolated database batch for a full chat rerun. This workflow does not upgrade the primary installation or deploy remotely.

For a fresh chat rerun, stop and remove only `hugagent-application-e2e-backend` and `hugagent-application-e2e-mcp`, then run setup with `APPLICATION_E2E_BATCH=run2`. Previous batch databases remain intact and drivers keep the same loopback URLs. Do not remove primary containers or databases.

## Desktop integration

Local-only startup provisions a separate applications.sqlite store. Static pages do not create applications.
Local MCP addresses use the configured backend origin and are reachable only on that device.
Stopping the local service also stops its MCP endpoints.

Dual mode hosts official sites, form data and MCP services together on the cloud.
Local tasks forward manage_application through the current account's authorized Sites gateway.
Failures never fall back to local storage. Both shells ignore stale local routing flags for hosting routes.
UI links and tool receipts use the stable backend origin, not the desktop window's temporary port.

Desktop SQLite startup does not execute the complete Alembic chain. It calls the shared verified
legacy retirement check before schema reconciliation. Unverified legacy rows are retained and do not prevent startup. Verified retirement still requires a successful archive.
Deliver an updated desktop package; dual mode also needs updated cloud backend/Sites MCP and capability sync.

Desktop upgrade backups include storage/applications.sqlite and WAL/SHM companions. Failed upgrades restore the original set and remove newly created companions.

## Existing site migration

Public record reads are disabled by default. Shared tables can explicitly enable public_read; whole-list replacement also requires public_replace and the current revision. Stale revisions return 409. Site visibility and password rules apply to reads and writes. JSON columns store arrays or objects; unique/indexed flags and query filters are unavailable for JSON columns.

Nonempty legacy tables require a matching verified SQL migration receipt before retirement. A desktop without that receipt retains its old tables and continues startup. Existing page APIs still need conversion. The application_site_migration command runs a preflight by default; MIGRATION_APPLY=1 applies the reviewed conversion. The current converter covers only the inventoried templates and stops on unknown pages or keys. application_site_verification validates source fields, site access rules, hosted files and source backups before producing a verified receipt. Local verification does not upgrade a remote environment.


## Publish to personal MCP through conversation

Sites 1.7.0 adds the `mcp-builder` skill and a dedicated `publish_mcp` tool.
Ask: “Create an enterprise query MCP from the industry knowledge center and add it to my personal MCP.”
Use `manage_application` to create the database, define fields and import real records, then call
`publish_mcp(app_id, tools)`. The server verifies connectivity and installs an owner-only personal MCP.
Credentials are encrypted in platform storage and excluded from tool receipts. The administrator must enable `can_add_mcp`.
Personal installation succeeds only when both `installed` and `connection_verified` are true.

Repeated publication updates the same personal entry. Management updates and rollbacks rotate its credentials;
revocation disables the entry and clears credentials. Publication and registration use separate database transactions.
A failed probe or registration returns a partial result: publication succeeded, personal installation did not.
Retry with the original app_id to repair it. Concurrent publication and revocation are serialized per application.
External clients still obtain credentials through application management. Docker connections use a backend-internal
address; this is not an external client URL. Receipts contain the public service path.
Desktop hybrid mode forwards through the current cloud account. The tool accepts no caller-provided connection URL
and does not relax private-network restrictions for generic MCP imports.
New personal tools load on the next conversation turn; the current tool list need not update immediately.

## MCP project editing

Create with `manage_application(action="create", payload={"title":"Service name","kind":"mcp"})`.
The backend provisions a personal project containing `mcp.json`. Data-only applications default to
kind=data and do not provision projects. Legacy MCPs acquire a project on publication or Edit.
Repeated editing reuses the project and its conversation; deleted projects produce an explicit error.

The file contains app_id, version and tools. Saving a draft does not change the live service.
Use `manage_application(action="publish_project", app_id="<original ID>")` or the owner endpoint
`POST /v1/applications/{app_id}/mcp/project` to publish it. Publication updates the same service URL
and personal connection, retaining deployment history. Wrong identities, stale base versions and
invalid tools are rejected. The source action returns both the draft and published_definition;
reconcile changes before advancing the base version rather than bypassing conflicts.

`POST /v1/applications/{app_id}/editor` returns the project, source file, draft and published configuration,
without secrets. File writes use existing project-source revision checks. When publication succeeds
but a concurrent draft write prevents synchronization, project_synced=false preserves that draft and
reports partial completion. Reconcile its base version before editing further. Desktop dual editing
uses cloud projects and cloud conversations, matching hosted MCP identity and publication.

Before switching an existing deployment to this code, rerun `python -m core.services.application_hosting_setup`.
Provisioning idempotently adds hosted_application_sources without modifying business tables, records
or credentials. No platform Alembic migration is needed. When reverting the code, retain the registry
and project files so subsequent upgrades reuse bindings; do not delete application data.

Management and rollback publication reject an existing unpublished project draft instead of overwriting it. Project publication waits for file-tool registration and refreshes the mounted source to its new version before reporting synchronization complete.

## External review fixes and upgrades

Application hosting remains optional. Set different APPLICATION_DB_PASSWORD and APPLICATION_OWNER_DB_PASSWORD values. Use both docker-compose.yml and docker-compose.application-hosting.yml.

Run application-database-init for new deployments and existing volumes. It creates recovery history and transfers application schema and table ownership to application_owner. It also grants access to existing application roles. Initialization failures must block backend startup.

The backend uses application_owner with NOSUPERUSER, NOCREATEDB and CREATEROLE. application_admin is bootstrap-only. CREATEROLE permits provisioning and reclaiming application roles. Use a dedicated PostgreSQL instance, never the platform database.

Visitor API successes and errors include CORS headers. Cloud writes and password attempts share atomic Redis counters across workers. Missing or unavailable Redis returns503. Only local single-process mode without Redis uses memory counters.

public_replace defaults to false. Enabling it permits anonymous visitors to replace or erase the entire table. Revisions and rate limits cannot prevent the first erasure.

Replacements atomically retain the previous contents. Each table keeps20 snapshots: a protected first nonempty baseline and the latest19 versions. Anonymous replacements cannot evict the baseline. Each snapshot is bounded to512 KiB; oversized snapshots reject replacement.

Owners can restore through management using the observed current revision. Stale revisions return409. Restored records receive new IDs.

Owners can delete records, tables and applications. Record DELETE requires version. Erasure removes table recovery history and application deduplication receipts. Tables referenced by MCP tools return409 until those definitions are removed.

Application deletion revokes tokens and removes the personal connection. It deletes the dedicated schema, role and registry entries, reclaiming quota. Source project files remain independently managed. Database backups follow the operator retention policy.

CSV streams the entire table from a consistent transaction snapshot. JSON uses standard syntax; spreadsheet formula protection remains active. MCP GET, HEAD and DELETE return405. Only POST carries stateless protocol requests. Tool schemas constrain limit to1–100.
