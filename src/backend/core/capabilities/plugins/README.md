# Plugin capabilities

This package owns plugin manifests, installation records, capability selection and UI contracts.

| Package | Responsibility |
| --- | --- |
| `management/` | Install, enable, uninstall, inspect and publish database-backed plugins |
| `packaging/` | Discover files, extract archives and normalize manifests |
| `runtime/` | Resolve deferred capabilities and retain per-chat activation state |
| `ui/` | Validate UI declarations, project browser-safe data and authorize data queries |

Framework-specific activation belongs to `core.llm.tools.plugin_activation`.
Device-local installation and migration belong to `features.desktop.local.plugins`.
Marketplace publication policy belongs to `features.marketplace`.

Installation and packaging must not import the agent factory or SDK activation.
Packaging does not publish database records: archive extraction belongs to
`packaging/archive.py`, while installation persistence belongs to `management/`.

Use explicit owner imports and patch the consuming interface in tests. The old
`core.plugins` and `core.services` modules are removed; no compatibility exports
are provided. Bundled resource paths are relative to the backend root in local
and Docker installations.
