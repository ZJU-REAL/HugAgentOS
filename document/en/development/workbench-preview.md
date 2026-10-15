# Workbench developer preview

[中文](../../zh-CN/development/workbench-preview.md) · [SDK](workbench-sdk.md)

Use the private SDK artifact to develop outside the host repository.
The artifact includes README, API, VERSIONS, CHANGELOG, developer tools and the workspace example.
It requires no host source, React store or native desktop API.
Public registry publication requires separate authorization.

## Independent project

1. Unpack the SDK tarball supplied by your administrator.
2. Copy `package/examples/workspace/` into a separate project.
3. Install the same tarball into that project.
4. Build and validate the plugin.

```sh
npm install --ignore-scripts --offline /artifacts/hugagent-workbench-sdk-1.0.0.tgz
npm run build
npm run validate
npm run preview
```

Node 20 or later and cached esbuild 0.27.3 are required for an offline build.
The SDK tarball has no runtime registry dependencies.
Provision compiler tools through your approved process if the offline cache is missing.
The build bundles the SDK into self-contained HTML. No CDN or host import is used.

## Permission preview

Open the printed 127.0.0.1 URL. The preview has no iframe until you click **Open example**.
Select allowed, denied or missing-capability permissions.
Open the example. Change theme and language. Select Overview and Files.
Use browser history and refresh to inspect view restoration.
Query the sample source, pick a simulated file reference and save that reference.
The denied scenario must show capability_denied. The incompatible scenario must show host_incompatible.
Closing removes the iframe and its channel.
Changing permission scenarios closes the old session before opening another one.

The preview is a protocol simulation. It contains no production account or backend.
It does not read actual files or perform real data queries.
Its server binds only to loopback and serves exact preview routes.
The frame has an opaque origin and cannot fetch external resources.
Do not use it as an authentication bypass or as evidence of real authorization.

## Real host exercise

Replace the sample data-source URL with a controlled endpoint approved by the administrator.
Keep upstream authentication on the server. Do not put credentials into HTML, logs or URL query strings.
Create a ZIP containing dist/plugin.json and dist/web/ at its root.
Import it with the host's plugin manager.
Discover the exact applicationKey from the authenticated catalog.
Open the plugin. Approve its declared capabilities through the host.
Exercise both views, refresh and browser history.
Query the approved source, select a temporary owned file, and confirm its save.
Repeat with denied grants, another account, a disabled installation and a stale revision.
Check that unauthorized calls fail and resources are released.

Real host data/files, target-system login and native-client results must be recorded separately.
The preview does not complete those checks.
Read [error codes](workbench-errors.md) and [migration](workbench-migration.md).

## CLI

`workbench validate <JSON> [--assets directory]` prints field paths and codes.
It accepts a standalone application declaration or a UI v3 plugin manifest.
It uses the canonical protocol validator, module references and bounded package-local HTML checks.
Host package validation remains authoritative for the complete plugin.

`workbench preview <plugin-directory> [--port 4179]` validates before serving.
Use port 0 to select a free local port.
Exit codes are 0 for valid input, 1 for invalid declarations and 2 for command or preview failure.
