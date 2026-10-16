# Workbench builds and offline artifacts

The workbench ships with the host frontend. Protocol and SDK remain separate private packages, currently version `1.0.0`. Workspace applications use plugin UI v3. Legacy UI v1/v2 remain supported. Both packages are `private: true` and `UNLICENSED`. This workflow does not publish to public npm.

## Build order

From the repository root:

```bash
npm --prefix src/frontend ci
npm --prefix src/frontend run build
node scripts/build-workbench.mjs --verify-dist src/frontend/dist
```

Frontend prebuild validates manifests, locks, licenses and protocol versions. It builds protocol, then SDK, using the frontend's locked TypeScript/esbuild tools. It also checks the backend Schema copy. Shared packages need no separate tool installation. Postbuild writes `dist/workbench-build.json` and `dist/licenses/`. Diagnostics contain relative artifact paths, versions and SHA-256 values. They contain no credentials or developer machine paths.

Missing sources, licenses, stale locks, incompatible versions, missing outputs and stale artifacts fail the build. Error prefixes include `workbench_missing_file`, `workbench_lock_drift`, `workbench_version_mismatch`, `workbench_stale_artifact` and `workbench_distribution_drift`. Fix the source and locks, then rebuild. Do not ship old dist by skipping these checks.

## Docker and CE

The frontend Dockerfile uses the repository root as context: `docker build -f src/frontend/Dockerfile .`. Its dedicated ignore file allows frontend, protocol, SDK, build scripts and backend Schemas. The image uses the same build order. Final web assets retain shared package licenses.

CE retains the base workbench and both packages. Organization features and internal documents remain excluded. CE uses `ce/overlay/src/frontend/package-lock.json`, matching its pruned dependencies. Docker and desktop use `npm ci`. After frontend dependency changes, regenerate this lock in a complete derived CE tree with `npm install --package-lock-only --ignore-scripts`, then copy it back to the overlay. Do not replace it with the EE lock. Existing edition gates remain in place.

## Independent offline packages

```bash
node scripts/pack-workbench.mjs ./artifacts/workbench
```

This command builds both packages, then creates two npm tgz archives and `workbench-artifacts.json` in the selected directory. The manifest records versions, archive SHA-256, artifact hashes and compatible UI versions. Consumers use local tgz files without a host checkout or CDN. Packing requires no dependency downloads when locked frontend tools are already installed.

## Desktop

Existing Tauri and UOS entry points retain the standard desktop preparation flow. Full bundles build desktop and CE login web assets, verify both manifests, then remove build dependencies. Before creating `server-ce.zip`, the archiver verifies shared packages, web licenses and the frontend lock hash. Missing or changed artifacts fail immediately. End users do not run Node or npm builds.

Follow the [desktop guide](../../../desktop/README.md) for native builds, signing and installation tests. Web builds and archive checks do not replace real Windows, macOS or UOS installation and offline acceptance.
