# Workbench protocol

This private package defines application declarations for HugAgentOS. It is not published to npm.
The package keeps the internal repository's licensing status (`UNLICENSED`). No public license is granted here.

```sh
npm ci
npm test
npm pack
```

Install the resulting tarball in an independent project. Import `validateApplication`,
`validateContributions`, `ApplicationDeclaration`, `ApplicationTarget`,
`PROTOCOL_VERSION`, `UI_VERSION`, and `HOST_CAPABILITIES` from
`@hugagent/workbench-protocol`. Existing UI types are exported from `/legacy`.
Versioned Schema files and positive/negative fixtures are exported from `/schemas/*` and `/fixtures/*`.
The built runtime bundles its validator; consumers need no host source, React, CDN, or validation service.

```ts
import {validateContributions} from '@hugagent/workbench-protocol';
const result = validateContributions({
  modules: [{id: 'main', entry: 'web/main.html', surface: 'workspace'}],
  applications: [{
    schemaVersion: '1.0.0', id: 'reports', title: 'Reports',
    target: {kind: 'plugin', moduleId: 'main'},
    compatibility: {protocolVersion: '1.0.0', requiredCapabilities: ['workspace']},
  }],
}, {source: 'plugin'});
console.log(result.errors); // only field paths, fixed codes and messages
```

Applications use UI v3. UI v2 already includes the resource bridge and is not repurposed.
Old UI v1/v2 tool, canvas and module behavior stays on the existing host parser.
Declaring a workspace module alone does not implement an application host or grant resource access.

Targets are discriminated: builtin/componentId, plugin/moduleId, external/url+mode, command/commandId.
Builtin components and commands require explicit trusted allowlists. Plugin contributions can reference
only their own workspace modules. IDs must be unique within their contribution kind; all entries with
a duplicated ID are rejected. Invalid modules withdraw dependent applications without suppressing healthy ones.
Navigation views have unique IDs and relative subpaths, and cannot contain nested views.
Module assets and application icons stay under `web/`; paths cannot contain traversal, encoded segments,
absolute paths or backslashes. External URLs must use HTTP(S) without credentials.
Unknown optional application fields are ignored during projection; mixed target fields are rejected.

Schema version 1.0.0 is exact. Compatibility protocol versions accept major 1, minor 0 and any patch.
An unsupported required capability rejects the application; unknown optional capabilities can be ignored.
Pass the actual `capabilities` array to validate a specific host. Declaration validity never grants permission.

The host builds this package before TypeScript/Vite. `build.mjs --host-tools` uses the frontend's pinned
build tools and creates no package dependency cache. Standalone builds use this package's own lockfile.
Python consumes byte-identical checked-in Schema artifacts under `core/applications/schemas`.
After editing Schema, run `node scripts/sync-workbench-schema.mjs` from the repository root.
Host builds and parity tests reject artifact drift.
