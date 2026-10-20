# Local and cloud skill and plugin management

The `skill-manager` and `plugin-manager` plugins own their management tools. Tools become available through the installed plugin, not as unconditional built-ins. There are no enable/disable manager tools; existing page switches remain.

## Installation and lifecycle

| Operation | Local execution | Cloud execution |
| --- | --- | --- |
| `install_skill` / `install_plugin` | Validate an absolute directory or ZIP/TGZ and publish to the device store and registry | Validate a package artifact owned by the current account and install privately |
| `list_*` / `get_*` | Read device installations and revisions | Read private cloud installations |
| `update_*` / `uninstall_*` | Exact installation ID and `expected_revision` | Exact installation ID and `expected_revision` |
| `upload_skill_to_cloud` | Available with a bound cloud account; upload and privately register a standalone skill | Not exposed; the skill is already in the cloud |

Local input:

```json
{"source":{"kind":"local_path","path":"/absolute/path/to/my-skill"}}
```

Use actual Windows absolute paths on Windows. Local installation needs neither `/myspace` nor `sandbox_get_artifact`.

Cloud input:

```json
{"source":{"kind":"artifact","artifact_id":"owned-cloud-package-id"}}
```

Read the current installation before updating. Submit its exact ID and revision with a complete replacement package. A changed revision rejects the write. Manage plugin-owned skills through the whole plugin.

## Pages and runtime

Local tool installation, page uploads and the skill editor share one installation service. The skill page shows standalone skills; the plugin page shows plugins and their child skills/connectors. Successful tools refresh shared page state without reopening the app.

The desktop provides `HUGAGENT_CAPS_ROOT`. Immutable versions live under `skills/local/<key>/<revision>` and `plugins/local/<key>/<revision>`. Installation snapshots and validates input before publishing the registry entry. Uninstallation revokes the entry while retaining input files and historical versions. Directory discovery reports pending imports; it never installs implicitly.

An installed package may still require a runtime, connector or account connection. Check readiness separately. Inline connector credentials are rejected; use the dedicated connection configuration.

## Cloud upload, marketplace and preview

`upload_skill_to_cloud` retains the local installation and creates a private cloud skill. The first upload binds a stable cloud ID; retries reuse a durable request identity. Updating that copy requires `expected_cloud_revision` to avoid overwriting changes made elsewhere.

Marketplace submission is a separate review request using a cloud skill ID. Uploading is not publication.

`pin_to_workspace` delivers a file preview. It neither installs the package nor moves source files. Local paths, workspace preview references and cloud artifact IDs are different identifiers.

## Upgrade

Official manager bundles use version 2.0.0 and declare a versioned executor contract. Local execution requires a matching installed declaration. Unknown contracts require an upgrade and never fall back to cloud mutation.

Startup migrates legacy personal skills and device plugins into the device registry, retaining encrypted rollback snapshots. Official local managers receive updated creator instructions and tool declarations. Failed migration preserves original database records. Legacy plugins carrying inline credentials require secure reconfiguration before migration. In hybrid mode shared default plugins continue to come from authenticated cloud synchronization.

### Troubleshooting cloud installation

Cloud upgrades must apply database migrations (the standard backend entrypoint does this) and update both backend and MCP services. Migration `capids01` widens `content_blocks.id` to 128 characters while preserving existing skill revision, working-copy and installation receipt IDs and contents. If installation returns `Database field length limit exceeded (SQLSTATE 22001)`, check migration completion instead of repackaging the skill. Other database failures report SQLSTATE when available without exposing SQL or bound parameters.

Downgrades stop if any IDs exceed 64 characters, preventing revision and receipt truncation. Back up the data and plan a migration for these records before downgrading; do not delete or truncate records to force a downgrade.

## Update or select a version in marketplace details

Administrators open a skill, plugin or agent marketplace detail and use Update version
and the version selector beside its title. Connectors use the same controls in the
Config console MCP marketplace detail. There is no separate capability version page.

Download the current package, edit its contents, and upload a ZIP. Skill packages
contain SKILL.md and plugins retain their existing manifest format. Agent packages
contain only agent.json; connector packages contain only connector.json. Resource
identity must remain unchanged. The backend chooses patch +1 by default, with minor
and major increments available. Uploaded version fields do not determine the release.
A legacy nonnumeric version starts managed versioning at 1.0.1.

Limits: ZIP 20 MiB, extracted total 50 MiB, individual file 10 MiB, at most 1000 entries.
Traversal, links, duplicate paths, encrypted archives, excessive compression and
credential files or inline credentials are rejected. Agent binding changes and MCP
endpoint/auth/tool-definition changes continue through the existing editor, review
and revalidation flows; version packages cannot bypass those controls.

The selector shows the current version and at least five historical versions.
Selecting and confirming restores complete contents and the original version number.
Subsequent uploads increment the highest published version. The current version from the existing
publisher is retained before the next update or selection of an older snapshot. Built-in skill changes
update runtime contents; existing installed copies of other market resources remain
under their established installation and configuration flows.

## Default interactive visualization plugin

The bundled visualize plugin installs once at CE, EE, and local-service startup, including existing databases. Local installs respect the installer default-plugin switch; hybrid mode continues to use authenticated cloud capability synchronization. Later removal is respected, and existing disabled settings are preserved.

Use it for charts, simulations, explanations, maps, and interface mockups. Complete HTML documents open through existing file cards and the Canvas preview. Common chart libraries are embedded during export and require no account. Interaction state remains local to the page and never reaches the model; the preview sandbox may prevent persistent storage. Use the sites plugin to publish a website.

## Plugin account connections

Plugins declaring an account connection (Yida, Lark, DingTalk, and email) show
connection status and login controls in their detail page. Local mode manages the
account on this computer. Hybrid mode offers Local and Cloud selectors. Each
location has its own login; status, login, polling, and disconnect requests stay
on the selected location. Capability synchronization does not copy credentials.

Installing a plugin does not install its runtime dependencies. Local login needs
the corresponding CLI; Yida also needs Node.js and Bash. Updated full desktop packages include pinned OpenYida and locked dependencies,
using the private Node already shipped with the browser runtime. No system Node
or npm installation is needed. Other plugins retain their CLI prerequisites;
standalone local deployments can use an installed CLI. Cloud connections use
server dependencies. An unavailable local service never redirects
login to the cloud.

Local Yida login stores cookies in the current user's persistent directory.
Skill instructions read in later local conversations use that same directory.
Disconnect removes login cookies for that user without clearing other users or
cloud connections.

This fix requires updated cloud backend and full desktop packages. Resynchronize
older cloud plugin snapshots with Sync cloud capabilities. Update older locally
imported plugins from their original package to retain the connection declaration.
There is no need to delete accounts or the database.
