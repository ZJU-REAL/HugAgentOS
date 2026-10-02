# Plugins

Plugin code has one domain owner: this package. Neither `core/services/` nor
`core/llm/` contains another plugin implementation.

| Package | Responsibility | Main entry |
| --- | --- | --- |
| `management/` | Install, enable, uninstall, inspect and publish database-backed plugins | `core.plugins.management` |
| `packaging/` | Discover files, extract archives and normalize plugin manifests | `importer.normalize_plugin_dir`, `archive` |
| `runtime/` | Resolve deferred capabilities, activate tools/skills and restore per-chat activation | `core.plugins.runtime` |
| `local/` | Device-local installation and migration of managed bundles | `local.service` |
| `ui/contract.py` | Normalize UI declarations and project browser-safe data | `normalize_ui`, `public_contributions` |
| `ui/declarations.py`, `ui/primitives.py` | Contribution-specific parsing and shared validation rules | Internal to UI ingestion |
| `ui/contributions.py`, `ui/data_proxy.py` | Resolve installed UI and execute authorized data queries | API routes call these explicitly |

Runtime code may read installation state; installation and packaging must not
import the agent factory or plugin runtime. Packaging does not publish database
records: safe ZIP extraction lives in `packaging/archive.py`, while marketplace
persistence lives in `management/packages.py`.

Add a new manifest format in packaging, lifecycle behavior in management, and
activation behavior in runtime. Add UI declaration kinds in declarations and
host renderer support together. Do not add another `plugin_*` sibling to the
LLM or general services directories. Internal imports and test patch targets
use the new owner paths; old module paths are intentionally removed.

Bundled resource paths are resolved relative to the backend root, including
in Docker. Package discovery uses the existing `core*` setuptools inclusion.
