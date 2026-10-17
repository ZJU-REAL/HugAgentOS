# Object Storage

> Last updated: 2026-06-11

HugAgentOS funnels all persistent files — user uploads, AI-generated artifacts, knowledge base documents, exported reports — through a single **storage backend abstraction**. The `STORAGE_TYPE` environment variable switches between three implementations: local filesystem (Community Edition default), S3-compatible object storage, and Alibaba Cloud OSS (cloud storage is Enterprise Edition, EE — feature flag `cloud_storage`). Business code programs against the protocol only, so swapping backends requires zero code changes.

## Protocol and factory

The storage protocol is `src/backend/core/storage/protocol.py::StorageBackend` (ABC) with 7 methods:

| Method | Description |
|---|---|
| `upload(file_path, storage_key)` | Upload a local file, returns the storage URL |
| `upload_bytes(content, storage_key)` | Upload raw bytes |
| `download(storage_key, local_path)` | Download to a local path |
| `download_bytes(storage_key)` | Download as bytes |
| `generate_presigned_url(storage_key, expires_in=900)` | Generate a presigned direct-access URL |
| `delete(storage_key)` | Delete |
| `exists(storage_key)` | Existence check |

The factory is `core/storage/factory.py`: `get_storage()` returns a lazily initialized process-level singleton; `get_storage_backend()` dispatches on `STORAGE_TYPE`. Cloud backend imports are kept inside their branches — when the CE-derived tree physically lacks `s3.py` / `oss.py`, the factory still works under the `local` configuration.

```python
from core.storage import get_storage, generate_storage_key

storage = get_storage()
key = generate_storage_key(env="dev", user_id=uid, category="uploads", filename=name)
url = storage.upload_bytes(data, key)
```

### Storage key scheme

`generate_storage_key()` produces standardized keys:

```
{env}/{category}/{user_id}[/{chat_id}]/{timestamp}_{filename}
```

`category` is mapped from the resource type (`get_storage_category_for_resource()`): `artifact→artifacts`, `kb_document→kb_documents`, `upload→uploads`, `export→exports`, `temp→temp`. Filenames are cleaned via `secure_filename`, and the local backend adds path traversal protection (out-of-bounds keys are diverted into a hashed `sanitized/` directory, see `local.py::_get_full_path`).

## The three backends

### local (Community Edition default)

`core/storage/local.py::LocalStorageBackend` — writes under `STORAGE_PATH` (default `./storage`, typically mounted as `/app/storage` in containers). `generate_presigned_url` returns a local path (no real signing semantics); suited to development and single-node deployments.

```bash
STORAGE_TYPE=local
STORAGE_PATH=./storage
```

### s3 (Enterprise Edition, EE)

`core/storage/s3.py::S3StorageBackend` — boto3-based, compatible with AWS S3 and S3-protocol services such as MinIO (signature v4, 3 retries).

```bash
STORAGE_TYPE=s3
S3_BUCKET=your-bucket            # required
S3_ENDPOINT=https://s3.amazonaws.com   # optional; set for S3-compatible services
S3_REGION=us-east-1
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_CDN_DOMAIN=cdn.example.com    # optional CDN-accelerated domain
S3_PRESIGNED_URL_EXPIRY=900      # presigned URL validity (seconds)
```

### oss (Enterprise Edition, EE)

`core/storage/oss.py::OSSStorageBackend` — Alibaba Cloud OSS via the oss2 SDK, with optional unified key prefixing.

```bash
STORAGE_TYPE=oss
OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com   # required
OSS_BUCKET=your-bucket                              # required
OSS_ACCESS_KEY_ID=...                               # required
OSS_ACCESS_KEY_SECRET=...                           # required
OSS_KEY_PREFIX=hugagent/        # optional bucket-wide prefix
OSS_PRESIGNED_URL_EXPIRY=900
```

## Artifact store (AI-generated outputs)

Files produced by MCP tools (reports, charts, documents, …) go through `src/backend/core/artifacts/store.py`, a lightweight artifact repository layered on top of the protocol:

- **Dual mode**: with `STORAGE_TYPE=local`, bytes land in `{STORAGE_PATH:-result}/artifacts/`; with `oss`, they are uploaded to OSS and only an index entry is kept locally;
- **One file per record**: metadata lives at `{base}/artifacts/records/<first-two>/<file_id>.json`, so registering and looking up an artifact each touch a single small file and cost the same no matter how large the install grows; in OSS mode every record is mirrored to `artifacts/_records/<first-two>/<file_id>.json`, and a lost local volume is rebuilt with `core.artifacts.store.restore_records_from_oss()` (a maintenance operation, never a request path);
- **Legacy index migration**: older installs kept every record in a single `{base}/artifacts/index.json`, so adding one artifact rewrote and re-uploaded the whole index and every lookup re-parsed it. The backend splits that file into the per-record files above on startup (one-time and restartable), renaming the original to `index.json.migrated` for reference;
- **SVG auto-fit**: saved SVGs get their viewBox expanded automatically (`core/content/svg_fit.py`) so model-generated diagrams are never clipped;
- Note: this index repository's cloud branch only recognizes `oss`; under `STORAGE_TYPE=s3`, artifact outputs fall back to local-mode persistence (the generic storage protocol layer is unaffected).

## Upload / download paths

```
Upload                                    Download
──────                                    ────────
POST /v1/file/upload                      GET /files/{file_id}
  · 50MB cap                                · mode=direct|presigned
  · key: {env}/{uid}/user_uploads/          · inline=true for inline display
         {artifact_id}/{filename}           · authz + audit (file.download)
  · storage.upload_bytes()                GET /files/{file_id}/preview
  · writes the artifacts DB row             · Office → PDF online preview
  · returns download_url=/files/{file_id}
```

- **Upload**: `src/backend/api/routes/v1/file_upload.py` — persists to object storage and writes the `artifacts` table (ORM `core/db/models/artifact.py::Artifact`, including `storage_key` / `user_folder_id` / `team_id` ownership fields); supports targeting a personal folder (MySpace);
- **Download**: `src/backend/api/routes/files.py` — `/files/{file_id}` is a **non-v1** legacy-stable path (kept for artifact URL compatibility), supporting direct / presigned modes, inline display, ownership authorization, and audit logging;
- Knowledge base document uploads use a separate route (100MB cap); see [Knowledge Base](./knowledge-base.md).

## myspace_cache and sandbox write-back

A backend mirror cache sits between MySpace files and the code sandbox (details in the [sandbox module](./sandbox.md)):

- Cache directory: `{STORAGE_PATH}/myspace_cache/{user_id}/...` (`core/sandbox/_common.py::myspace_cache_dir`); team files have a shared `team_cache_dir(team_id)`;
- **Seed**: when a persistent sandbox session is first created, cache files are seeded into the sandbox at `/workspace/myspace/{user_id}/`, then incrementally synced by mtime;
- **Lazy loading**: when sandbox-side Read/Glob/Grep miss a file, the path is resolved to an artifact, downloaded on demand from object storage, and materialized into the sandbox (`core/llm/tools/myspace_vfs/__init__.py::materialize_into_sandbox`);
- **space_delete / space_move / space_create_folder**: these three are commands issued *against* My Space (the same class as the UI's buttons), so the tools update the `artifacts` table and the myspace_cache mirror directly;
- **Real-time registration**: any write or delete under the mirror directory is registered back into the `artifacts` ledger by `core/myspace/watcher.py` — **regardless of who wrote it** (tools, `bash`, background processes, sub-agents and skill CLIs alike). New files are registered and shown directly; edits to or deletions of the user's existing files go through the confirmation gate (a decline restores that version from object storage); files the user deleted are never resurrected. See [Sandbox](./sandbox.md).

Object storage is therefore the **single source of truth**; myspace_cache is just an acceleration mirror that can be wiped and rebuilt at any time.

> ⚠️ With `OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED` on, the mirror directory *is* the `/workspace/myspace/{uid}` the sandbox sees, so "the file is in the mirror" does **not** mean "it is registered in My Space" — always decide from the `artifacts` records. Confirm registration has completed before wiping the mirror, or unregistered files are lost for real; use `scripts/reconcile_myspace_mirror.py` once to settle a backlog.

## Best practices

| Scenario | Recommended setup |
|---|---|
| Local development / single-node trial | `STORAGE_TYPE=local` (default), mount `STORAGE_PATH` as a persistent volume |
| Production (Enterprise Edition, EE) | `STORAGE_TYPE=oss` or `s3`, enable presigned direct downloads to offload backend bandwidth |
| Multi-replica deployments | OSS/S3 required — the local backend does not share files across instances |
| Offline production | local + host volume mount (`HOST_STORAGE_PATH`, see [offline production deployment](../deployment/offline-production.md)) |

## Source map

| Path | Responsibility |
|---|---|
| `src/backend/core/storage/protocol.py` | `StorageBackend` abstract protocol |
| `src/backend/core/storage/factory.py` | Factory, singleton, storage key scheme |
| `src/backend/core/storage/local.py` | Local filesystem backend (with path traversal protection) |
| `src/backend/core/storage/s3.py` | S3-compatible backend (Enterprise Edition, EE) |
| `src/backend/core/storage/oss.py` | Alibaba Cloud OSS backend (Enterprise Edition, EE) |
| `src/backend/core/artifacts/store.py` | Artifact repository (JSON index + local/OSS dual mode) |
| `src/backend/api/routes/v1/file_upload.py` | `/v1/file/upload` upload path |
| `src/backend/api/routes/files.py` | `/files/{file_id}` download / preview |
| `src/backend/core/db/models/artifact.py` | `Artifact` ORM (storage_key and ownership fields) |
| `src/backend/core/sandbox/_common.py` | `myspace_cache_dir` / `team_cache_dir` |
| `src/backend/core/llm/tools/myspace_vfs/__init__.py` | MySpace ↔ sandbox bidirectional sync layer |

Related docs: [Sandbox](./sandbox.md) · [Projects & MySpace](./projects-myspace.md) · [Environment Variables](../deployment/environment-variables.md) · [Edition Comparison](../editions/overview.md)

### Folder uploads and recovery

Personal and editable team spaces accept folder pickers and recursive drops. Drops preserve empty
directories and can include loose files. Directory pickers only expose directories containing files.

POST `/v1/myspace/folders/batch` or `/v1/teams/{team_id}/folders/batch` accepts up to 200 relative
`paths` and an optional `parent_folder_id`, returning a `data.folders` path-to-ID map. Existing
sibling directories are reused. No fixed folder depth is imposed. Invalid paths, cycles and access permissions are still checked; operating system path limits still apply. Each batch is atomic; earlier batches
remain if a later batch fails.

Files use a shared browser queue with four concurrent requests and at least 200ms between request
starts. HTTP 429 pauses the queue according to Retry-After, falling back to exponential backoff.
Network errors, 408 and 5xx also receive up to three additional attempts. Permission, size and
validation errors are listed as failures. Retried requests consume the same queue budget.

Tasks capture the account, API address and destination. Navigation cannot redirect an upload.
An optional UUID `upload_key` identifies each file attempt. Replays by the same user return the
previous file; changed content/destination or a moved, overwritten or deleted result returns 409.
Separate uploads with the same filename create separate records, preserving existing behavior.

Progress, failed paths and “Retry incomplete items” are shown in the panel. The current tab saves
the manifest in sessionStorage, without file bytes. After refresh, reselect the original files or
folder; completed files are skipped. Recovery after closing the tab is not guaranteed. Clearing
the upload record starts a new task and does not delete uploaded files. Persistence failures are
shown explicitly.

The existing 50 MB/nonempty-file limits and lazy parsing remain. This repair does not add chunked
uploads, ZIP extraction, cross-tab throttling or background transfers after closing the page.
Multiple users sharing an IP may still be throttled; the queue waits and retries.

## Duplicate filenames in team folders (EE)

Active filenames must be unique within each team folder, including the team root.
Uploads, pinned outputs, copies, moves and renames that introduce a duplicate return
409 with a request to choose another name. They never overwrite the existing file.
Different teams, different folders and personal chat outputs may share a name.
Editing by the original file ID or an existing sandbox path still updates that file,
with the existing concurrent revision checks.

On upgrade, the earliest file in each duplicate group keeps its name; other files
receive an ID suffix, also reflected in their display names. IDs, original bytes
and download references are retained. Copying a personal folder to a team rejects
duplicate source names before creating destination folders; rename them first.

## Browsing archive contents

Click an archive in My Space to view its folders and files in the right panel.
Open a folder to enter it. Use Parent folder or the breadcrumb path to return.
The search field filters the current folder.

Supported formats: ZIP, TAR, TAR.GZ/TGZ, TAR.BZ2/TBZ/TBZ2, TAR.XZ/TXZ,
and single-file GZ/BZ2/XZ. RAR and 7Z currently offer download only.
Nested archives can be downloaded but cannot be expanded further.

Click a member to preview text, images, PDF, or Office files. Existing preview
format and size limits apply. HTML and SVG are displayed as text. Members can
be downloaded individually. Browsing does not import members into My Space
or modify the original archive. Members of local archives are also downloaded
before opening outside the app.

List entries with `GET /files/{file_id}/archive`. Read a member with
`GET /files/{file_id}/archive/member?path=<relative-member-path>`.
Office PDF previews use
`GET /files/{file_id}/archive/member/preview?path=<relative-member-path>`.
All endpoints require authentication and the original personal/team file's
read permission. Deleted files cannot be accessed.

Limits: 100 MiB original archive, 5,000 entries, 8 MiB ZIP central directory,
256 MiB expanded TAR scan, 30 MiB per member, and 64 MiB XZ decoder memory.
Download and extract the original archive locally when a limit is exceeded.

Unsafe paths are hidden. Links, special files, duplicate or conflicting paths,
and encrypted members cannot be read. The panel explains these restrictions.
Invalid archives show an error and a retry action. Browsing is read-only and
does not extract the entire archive into the user's workspace.


### Space synchronization and permission changes

Synchronization tracks each path independently. A failed task retains its retry state without
blocking other registrations or reads of committed resources. Listings can return committed data.
Operations requiring complete consistency, such as application publication, retain strict barriers;
a barrier for selected paths only waits for related work and preserves local changes on conflicts.

Uploaded filenames must be single names; folder IDs specify their directories. Historical invalid
names are reported individually without stopping other cache downloads. The maintenance tool supports
preview, repair and rollback. Repairs retain file IDs and stored objects, preserving both records
under distinct names when paths conflict.

Subsequent tools in ordinary chats read the latest permission preference. Switching to full access
lets the server resolve permission waits for ordinary chats owned by the account. The interface closes each
prompt when its resolution event arrives. Account preferences do not override visitor or module-specific policies. Permission presets
do not replace resource access checks; questions and design choices remain interactive.
