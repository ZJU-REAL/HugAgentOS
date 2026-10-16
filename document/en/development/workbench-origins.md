# Workbench sources and offline installations

Same-name local and cloud plugins have separate entries labelled Local or Cloud. Launch, state and resource calls stay bound to the selected source. A failed cloud call never opens a same-name local installation.

## Exact identity

The directory returns `origin: {kind, id}`. kind is local or cloud. id is a persistent service identifier, not a URL or private path. Plugin applicationKey includes source, service identity, the full installation ID SHA-256, its UTC microsecond creation stamp, and the application ID. Names and slugs are not installation identities.

Launch returns the same origin, installId, packageRevision and executionScope. Use sourceRequest.workbenchRequest for scoped host calls. Instance-only endpoints must explicitly carry source and x-workbench-origin. SDK applicationSource(connection) reads the bound source; it does not grant access.

## Local, cloud and dual modes

Web uses the authenticated current hosting service source. Standalone local hosting is supported without access to another local service or native APIs. cloud_only uses cloud. local_only uses local. dual reads both directories. The local identity must be ready before local calls in dual mode. Each failed source reports its own reason. A failed directory is not an empty directory.

availability.executionScopes gates execution location; requiresOnline gates offline opening. Calls do not retry against another source. Restore the selected source or sign in again before retrying.

## Local privacy

Local and dual navigation preferences stay on the local backend. Cloud preferences reject local-only references. Preferences contain opaque applicationKey values only, never page definitions, private paths, credentials or file contents. Device preferences in dual mode do not automatically upload to cloud.

Account or service changes clear previous directory caches. Restore workbench_origin_identity for the same service. Generate a new identity when cloning to another service or device. Migration of old installation keys requires a confirmed source and exact installation record, never name matching.

## Offline pages

Bundle the SDK into the plugin's JavaScript. Use package-relative HTML, scripts, CSS, images and fonts without a CDN. The host embeds assets into an isolated page and continues to reject network connections, escaped paths and unbundled JavaScript imports. Missing assets return explicit package errors. Complete page assets do not make business data sources offline.

Native offline acceptance also checks the bundled runtime, valid local authentication, page readiness, data and files, resource cleanup and account switching.
