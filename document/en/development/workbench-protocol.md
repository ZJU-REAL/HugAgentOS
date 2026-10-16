# Workbench protocol

The independent `packages/workbench-protocol` package provides JSON Schema, TypeScript types,
validators, and positive/negative fixtures. It remains private and is not published to npm.

Run `npm ci`, `npm test`, and `npm pack` in the package directory. Independent projects can
install the tarball and import `validateApplication`, `validateContributions`, and
`ApplicationDeclaration` from `@hugagent/workbench-protocol`. The bundled validator needs
no host source, React, CDN, or validation service. The host frontend builds the package before its own build.

Set `extensions["org.hugagent"].ui.version` to 3. UI v2 already includes the resource bridge;
its meaning is preserved. Existing v1/v2 tool results, canvases, and modules use their original parser.
Add `applications` to `ui.contributes` and set the referenced module's `surface` to `workspace`:

```json
{
  "version": 3,
  "contributes": {
    "modules": [{"id": "main", "entry": "web/main.html", "surface": "workspace"}],
    "applications": [{
      "schemaVersion": "1.0.0", "id": "reports", "title": "Reports",
      "target": {"kind": "plugin", "moduleId": "main"},
      "compatibility": {"protocolVersion": "1.0.0", "requiredCapabilities": ["workspace"]}
    }]
  }
}
```

The host validates four target kinds: builtin, plugin, external, and command. Builtin components
and commands require trusted host allowlists. Plugin applications can reference only their own
unique workspace modules. Duplicate IDs, invalid module references, escaping paths, unsafe URLs,
and incompatible required capabilities are rejected explicitly. Errors contain field paths and fixed
reasons, without echoing rejected content. Damaged applications do not suppress healthy applications
or existing tool results.

Applications can declare navigation, one level of views, permissions, availability, lifecycle,
compatibility, and icon.light/dark. Module entries and icons use package-relative web/ paths.
The Schema version is exactly 1.0.0. Protocol negotiation accepts 1.0 patch releases and rejects
other major versions or newer minor versions. Unknown optional capabilities can be ignored.
A capability declaration never grants permission.

Both host languages run the package fixtures. Python consumes verified artifacts of the same Schema
without running a Node service. After editing Schema, run `node scripts/sync-workbench-schema.mjs`
from the repository root. Host builds check artifact equality. Frontend Docker builds use the
repository root as context and include the frontend and protocol package. Desktop frontend builds
use the same npm build entry point.

This package delivers declarations and validation. Application catalogs, page hosting, SDK methods,
and instance resources are implemented separately. Declaring a workspace module alone does not
start a page or grant native access.
