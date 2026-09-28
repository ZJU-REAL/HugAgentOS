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
