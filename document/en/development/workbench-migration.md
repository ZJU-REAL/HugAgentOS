# Workbench compatibility and migration

[中文](../../zh-CN/development/workbench-migration.md)

| Contract | Compatibility |
| --- | --- |
| SDK / protocol 1.0.0 | UI v3 workspace pages with negotiated capabilities |
| Existing UI v1 / v2 | Existing canvas and tool-result bridge |
| Existing business /v1/applications | Existing managed application domain |
| New /v1/workbench | Catalog, launch and application-specific operations |

SDK, protocol, UI and plugin business versions are separate.
Keep old UI declarations on their existing contract.
To add a workspace page, declare UI v3, a workspace module and an application that references it.
Bundle package-local web assets. Validate references and supported required capabilities.
Do not retrofit a workspace owner with a fabricated conversation ID.

Application keys are opaque catalog references.
They bind local/cloud execution scope, opaque service origin, full installation SHA-256 and UTC creation instant, including microseconds.
An upgrade retains the installation key. Reinstallation creates a new key.
Keep navigation and permission selectors on exact catalog keys.
Do not reuse grants by name, slug, service address or application ID.
A change of account, installation, execution source or revision requires fresh authorization.

## Safe migration procedure

1. Inventory old IDs, duplicates, explicit preferences, role inheritance, authentication and UI versions.
2. Take a consistent protected snapshot of configuration, data and installed packages.
3. Run read-only prechecks on a test copy. Isolate unknown entries and report them.
4. Preserve stable external mappings and existing explicit user preferences.
5. Keep visibility separate from business REST permissions. Do not expand authority.
6. Apply only the authorized migration, with revision checks and a receipt.
7. Re-run the operation to check idempotency in the test copy.
8. Test SQLite and PostgreSQL upgrade and recovery when those databases are used.
9. Verify real accounts, launch, data/files, revocation and existing user workflows.
10. Perform a production migration only under its separate authorization.

A failed read is not an empty preference set.
Keep unknown entries and error evidence. Do not delete them to make migration succeed.
After new writes, restoring only the old snapshot can lose data.
Freeze writes, export the increment and check old-format representation before downgrade.
If the old format cannot represent new data, stop the downgrade and restore the verified new version.
The SDK CLI validates declarations. It does not migrate databases or business data.

# Workbench configuration migration and recovery

The standalone migration command has been removed. The following sections describe internal migration and recovery constraints; they are not an executable operator procedure. Back up existing deployments, prepare a version-specific plan, validate it on an isolated copy, and obtain separate authorization before execution.

## Preserved behavior

- Existing external entries retain legacy:<id>. allowed_apps keeps its visibility domain. Missing/null personal values inherit; empty lists deny; * means all. Role union and team defaults remain unchanged. Visibility never becomes a business REST execution permission.
- External ordering and enabled state survive. Default navigation becomes layout policy. Existing modern preferences win. Confirmed old preferences.pinned_apps/app_order migrate to web preferences: null inherits and an empty list explicitly unpins. Chat order, theme, and unrelated preferences remain unchanged.
- Unmapped permission references remain unchanged and are reported. They do not grant access to new identities. Plugin UI v1/v2 is not upgraded to v3. Names and slugs never substitute for exact installations.
- Existing workbench_external_auth:legacy:<id> bindings retain their keys. Missing target adapters produce auth_configuration_required. Administrators must approve explicit login modes. Real legacy targets remain unverified until exercised. Generic SSO token forwarding must never return.
- Same-service recovery retains workbench_origin_identity. A service clone requires a fresh inventory and a regenerated service origin identity on the copy. Existing plugin keys do not automatically grant access to the new origin. Review cross-origin state and preferences separately.

## External compatibility window

content-v1 remains supported throughout workbench protocol 1.x. Announce a separate migration before protocol 2.0. Legacy app_config/page_config writes and content import/export use version adapters to write the canonical source directly. They do not retain a second table or dual-write configuration. Unknown fields fail explicitly. Administrator editors distinguish old and new entries while using the same durable storage.

## Recovery and incremental writes

Freeze all writers and follow the reviewed recovery plan.

Recovery first exports a protected delta. Changes to already-mapped ordinary legacy links can replay into the old format. New identities, application state, resources, permissions, plugins, authentication, or policy changes block unsafe downgrade with delta_not_representable. New data stays intact. Never restore only the old snapshot. Keep the verified newer version and review the delta. Recovery receipts are idempotent. A restored batch requires a new reviewed batch before another apply.

Validate representative database copies, PostgreSQL, SQLite, legacy login, both sidebars, and applicable native clients separately. Unit or HTTP fixtures do not establish complete upgrade acceptance.
