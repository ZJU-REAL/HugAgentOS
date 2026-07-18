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
- **JSON index**: `{base}/artifacts/index.json` maps `file_id → metadata`; in OSS mode the index is also backed up to OSS (key `artifacts/_index.json`) and restored automatically after container restarts;
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
- **Lazy loading**: when sandbox-side Read/Glob/Grep miss a file, the path is resolved to an artifact, downloaded on demand from object storage, and materialized into the sandbox (`core/llm/tools/myspace_vfs.py::materialize_into_sandbox`);
- **Reverse sync**: Write/Edit/Delete/Move tools propagate sandbox-side changes back — updating both the `artifacts` table (object storage) and the myspace_cache mirror, keeping the next seed consistent.

Object storage is therefore the **single source of truth**; myspace_cache is just an acceleration mirror that can be wiped and rebuilt at any time.

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
| `src/backend/core/llm/tools/myspace_vfs.py` | MySpace ↔ sandbox bidirectional sync layer |

Related docs: [Sandbox](./sandbox.md) · [Projects & MySpace](./projects-myspace.md) · [Environment Variables](../deployment/environment-variables.md) · [Edition Comparison](../editions/overview.md)
