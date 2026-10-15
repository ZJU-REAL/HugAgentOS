# Workbench SDK

Private browser SDK 1.0.0 for Workbench protocol 1.0.0 and plugin UI v3.
The package is private / UNLICENSED. Public npm publication requires separate authorization.
The SDK exposes no host source, React store, credentials, shell or native desktop API.

## Install an artifact

Use the SDK tarball supplied by your administrator. This package has no runtime npm dependencies.
Node 20 or later is required for developer tools. The page runs in a browser.

```sh
npm install --ignore-scripts --offline /artifacts/hugagent-workbench-sdk-1.0.0.tgz
```

Bundle the SDK into your own web assets. Do not load it from a CDN.
Read [API](API.md), [versions](VERSIONS.md) and [changes](CHANGELOG.md).

## Build the standalone example

Copy `examples/workspace/` from the unpacked SDK artifact into a separate directory.
The example contains no host-source imports. Its compiler is esbuild 0.27.3.
An offline build needs that compiler and its platform binary in the npm cache.
A missing cache must fail. Provision build tools through your approved process.

```sh
npm install --ignore-scripts --offline /artifacts/hugagent-workbench-sdk-1.0.0.tgz
npm run build
npm run validate
npm run preview
```

Open the printed loopback URL. Click **Open example**.
Select allowed, denied or missing-capability permissions. Test theme, locale and navigation.
Close the page to release its MessageChannel.
The preview contains simulated data and file references. It does not access accounts, production data or native files.
Preview results do not prove real host authorization.

Replace the example data source URL with an approved endpoint before real data testing.
ZIP `dist/plugin.json` and `dist/web/` at the archive root.
Import through the host plugin manager. Open the catalog's exact application key.
Use an isolated test account and a temporary owned file for the data/file exercise.
The example returns file references. It never transfers file bytes in messages.

## Validate declarations

```sh
npx --no-install workbench validate dist/plugin.json --assets dist
npx --no-install workbench validate external-application.json
```

The CLI outputs `{valid, issues:[{path,code,message}]}`.
Exit 0 means valid Workbench declarations. Exit 1 means invalid input. Exit 2 means command or preview failure.
Values are not echoed in diagnostics.
The validator checks canonical Workbench structure, references and package-local HTML paths.
The host still validates the complete plugin package and authorizes every operation.

## Build this package

Build the protocol package first. Install locked tools with your approved dependency process.
Run `npm run build`, then `npm pack`. Never run `npm publish` as part of this workflow.
Run `npm run test:tooling` for validation and `npm run test:consumer` for an independent offline consumer.
The consumer test creates a clean project outside the host checkout.
Set `WB21_CONSUMER_ROOT` to select a protected scratch directory.

# Workbench SDK

Private browser SDK, version 1.0.0. Bundle it into plugin web assets. It exposes no credentials, host stores, React dependency, or native API.

Call `await connect({requiredCapabilities: ['data.query']})` from a UI v3 workspace. The connection includes host identity, theme, locale, update subscriptions, and disposal. Required capabilities must be available before readiness. The host separately checks requested, approved, and effective grants for every privileged request.

The typed helpers are:
- `query(source, params, signal)`: request a controlled data source.
- `pickFile({accept}, signal)`: request the host file picker.
- `saveFile({fileId, name}, signal)`: request host saving of an owned file reference.
- `composeChat({text}, signal)`: fill a draft. This does not send a chat message.
- `openApplication({applicationKey, path}, signal)`: request host navigation.

The host confirms privileged actions in trusted UI. A plugin cannot supply approval or a trusted gesture. File references contain identity, name, size, and optional MIME type. The host consumes controlled HTTP resources; tokens, resource URLs, and file bytes do not travel through bridge messages. Hosts may reject unavailable methods or revoked grants.

`request(method, params, signal)` retains its existing method semantics, including distinct `chat.send` calls. JSON messages have a 256 KiB byte limit, 4096 nodes, and 16 nesting levels. Binary data, custom prototypes, accessors, sparse arrays, cycles, and non-finite numbers are rejected before cloning. A connection allows eight concurrent requests. Requests time out after ten seconds and send cancellation. Disposal and document exit reject pending requests. Late replies cannot settle another request.

Run `npm ci && npm test` to typecheck, bundle, install a private packed consumer, and test SDK bridge behavior through a MessageChannel. These tests use a simulated window; they do not certify native clients or privileged host UI.

## Bound source

`applicationSource(connection)` provides the exact opaque local/cloud origin. It is display context and grants no authority. State, resources, activity, and platform calls remain on the selected installation.
