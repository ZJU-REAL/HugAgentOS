# My Space file mounts

The web file manager and sandbox use the same authorized directories.

## Permissions

- Read: list, preview, read and download files.
- Edit: create, modify, move and delete all files in the authorized directory.
- Admin: edit files and manage member permissions.

The mount list excludes directories that the user cannot access.
Read-only directories use read-only mounts. Each file API request checks permission.
Before a permission change, the service closes the affected user's sandboxes.
If closure fails, the permission change fails.

## Storage

Full Docker deployments default to S3. Set STORAGE_TYPE to s3, oss or obs.
Cloud sandboxes require SANDBOX_PROVIDER=opensandbox.
CE Docker and desktop local mode use local storage.

The backend and controller must reach the storage endpoint.
The Docker host must provide `/dev/fuse` to the sandbox mount service.
The controller needs no host FUSE mount or shared-propagation directory.

## Local S3 service (CE / EE)

Add docker/object-storage/compose.object-storage.yml to COMPOSE_FILE.
Set S3_ENDPOINT=http://object-storage:9000 and S3_BUCKET=hugagent-files.
Generate separate random S3_ACCESS_KEY and S3_SECRET_KEY values.
Store credentials in a protected environment file with mode 600.
Build the backend image before startup. The initialization container creates a missing bucket and checks access.

The service uses a separate data volume. It does not publish host ports or use the Milvus volume.
Before switching an existing deployment, back up configuration and database.
Copy valid file objects and attachments. Check each object's SHA-256.
Keep source files. Switch STORAGE_TYPE and recreate the application containers after verification.

## Existing file migration

The standalone file import command has been removed. Back up the database and objects,
pause destination writes, and review a migration plan before moving existing files.
Check destinations, name conflicts, and content. Preserve source objects and references.

## Verification

Test upload, download and listing with an editor account.
Confirm that a read-only account cannot write or delete.
Revoke team access and confirm that the directory is unavailable.

Object-store directory moves use multiple object operations. They are not atomic filesystem renames.

## File page

The file page provides document lists, image views, folder navigation, and a team directory tree.
Listing, preview, search, upload, and rename use mounted directories available to the current user.
Read-only directories allow browsing and downloads. They do not show upload, rename, or delete actions.

Files open in Canvas. Text, Markdown and HTML support inline editing. Excel uses the spreadsheet editor. Save writes to the current space file. Read-only folders block saves. The server checks permission on each save.

File search returns paths in order. Pass next_cursor to continue with the same path and pattern. Each page checks permission and reads current directories. Pagination stores no file copies or search results. Global modification-time sorting is not supported. File additions, deletions and moves during a search can affect subsequent pages.

### Mount lifecycle and controller restarts

Each sandbox has one mount service for initial mounting and recovery. Object volumes are attached inside the sandbox namespace. Docker startup settings do not contain FUSE host binds. Synchronous Docker operations run in a thread pool.

Controller restarts preserve the independent mount service. Each file operation checks current permissions and live mounts. After a sandbox restart, the service reconnects authorized prefixes in the same container. Workspace files and read-only settings remain intact.

The mount service has no Docker socket. Credentials travel through a private input stream, not container labels or environment variables. Deletion and expiry remove its service. Recovery errors retain the sandbox and workspace.

Old sandboxes created with FUSE host binds need a separate data-preserving migration before upgrade. Their immutable Docker startup settings cannot be rewritten in place.

## Hybrid desktop MCP

Hybrid desktop sessions install the My Space connector from the cloud capability
manifest. Cloud Agent sessions continue using sandbox mounts directly. Local-only
sessions do not install this cloud connector.

The MCP interface lives in `src/backend/extensions/mcp_servers/space_files_mcp/`.
The built-in registry, `extensions/mcp_servers/registry.py`, declares installation placement
(`local`, `cloud`, or `both`) separately from exposure (`process` or
`desktop_gateway`). The space connector runs in the cloud through the desktop
gateway and does not open another listening port. Installation does not grant
account access: each gateway operation checks current directory permissions.

The connector exposes two tools:

- `space_files_read`: list roots, list directories, and read files.
- `space_files_write`: write files, create directories, move, and delete.

Read-only agents cannot discover or execute mutations, including forged calls.
Mutations use unified tool approval. Ask confirms writes and deletes. Auto approves
ordinary writes but still confirms deletes. Full uses the user's prior approval.
Read-only denial takes priority over every approval preset. Reads and writes use
Base64 and support at most 5 MiB; directories must be empty before deletion.

Gateway execution requires a live approval ticket matching the tool arguments.
Missing, expired, or changed-argument tickets cannot send a gateway request.
Desktop rollout requires the bundled backend, cloud backend, and a refreshed
capability manifest.

The object-storage image uses checksum-pinned binaries from the official MinIO GitHub release. Docker builds this image from docker/object-storage/Dockerfile.object-storage. Existing Milvus images and volumes are separate.
