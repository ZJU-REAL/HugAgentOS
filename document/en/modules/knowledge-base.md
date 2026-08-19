# Knowledge Base

> Last updated: 2026-07-19

HugAgentOS supports two knowledge base flavors that can run side by side and are presented together in the capability center:

1. **Self-hosted knowledge bases**: document upload → parent-child chunking → vectorization into Milvus → dense + sparse hybrid retrieval (RRF fusion) → optional reranking. Community Edition (CE) provides only private spaces owned by the current user. Admin-managed public spaces are an Enterprise Edition (EE) capability.
2. **External Dify knowledge bases** (Enterprise Edition, EE): with `KNOWLEDGE_BASE=dify`, the backend injects Dify datasets into the capability catalog at runtime, and retrieval goes through the Dify Retrieval API.

Both flavors are exposed to the agent as MCP tools: self-hosted retrieval uses `retrieve_local_kb`, Dify uses `retrieve_dataset_content` — both served by the same MCP server (`mcp_servers/retrieve_dataset_content_mcp/`).

In CE, the frontend shows only **Private Knowledge Base**, and `/v1/catalog` returns only private spaces owned by the current user. The backend rejects create requests with `visibility=public` and does not expose Dify or shared-knowledge-base service settings, so this boundary does not rely on frontend hiding alone.

## Architecture

```
                ┌──────────── Capability center /v1/catalog ─────────────┐
                │  api/routes/v1/catalog.py aggregates three kb sources:  │
                │  · private self-hosted spaces (CE + EE)                  │
                │  · Dify datasets (EE only, 60s in-process cache)         │
                │  · admin public spaces (EE only, system_public_kb)       │
                └──────────────────────────────────────────────────────────┘

  Ingestion (self-hosted)                   Retrieval (in conversation)
  ───────────────────────                   ───────────────────────────
  POST /v1/catalog/kb/{kb_id}/documents     Agent calls an MCP tool
    │  validate_kb_file (extension + magic)    │
    ▼                                          ├─ retrieve_local_kb (self-hosted)
  Object storage write (storage_key)           │    · embed_text(query)
    │                                          │    · Milvus hybrid_search:
    ▼  BackgroundTask                          │      dense (IP) + sparse (BoW) → RRF(k=60)
  core/content/kb_processing.py                │    · child hit → fetch parent chunk
  vectorise_document_background()              │      text from kb_chunks (PostgreSQL)
    · kb_parser.parse_and_chunk()              │    · optional reranker pass
    · (optional) LLM keywords / questions      │    · user_id isolation + global public KBs
    · embed_batch() → Milvus insert            │
    · parent chunks → PostgreSQL kb_chunks     └─ retrieve_dataset_content (Dify)
    · update kb_documents.indexing_status           · calls Dify /datasets/{id}/retrieve
                                                     · multi-dataset → sort by score, truncate
```

## Self-hosted knowledge bases

### Index modes: RAG / Wiki

Two index modes can be selected when creating a knowledge base, stored in `kb_spaces.metadata.index_modes`:

| Selection | `index_modes` | Chunking | Vectors / Milvus | Wiki generation |
|---|---|---|---|---|
| RAG retrieval only (default) | `["rag"]` | ✅ | ✅ | ❌ |
| Wiki graph only | `["wiki"]` | ✅ | ❌ | ✅ |
| Both → **LLM-Wiki knowledge base** | `["rag","wiki"]` | ✅ | ✅ | ✅ |

> All three modes write chunks to `kb_chunks`. Wiki citation works at chunk level and
> source lookup fetches chunks by ID, so it cannot exist without chunking — "Wiki only"
> skips vectorization, not chunking.

Legacy knowledge bases have no such key in their metadata, so they read back as RAG-only, matching the indexes they already have. See [Knowledge Base Wiki](./knowledge-base-wiki.md).

### Data model

ORM definitions live in `src/backend/core/db/models/knowledge.py`:

| Table | Description |
|---|---|
| `kb_spaces` | KB space: owner (`user_id`), `visibility` (private/public), `chunk_method`, document count / size stats |
| `kb_documents` | Documents: storage_key, checksum, `indexing_status` (processing / completed / failed) |
| `kb_chunks` | **Parent chunk** text (returned to the LLM on retrieval), with `tags` and related `questions` |
| `kb_assets` | **Media assets** carried by a document (figures cut out by layout parsing, standalone image uploads): bytes live in object storage; this table holds the owning chunk, the source position `locator`, the caption/OCR text `text_content`, and the model-generated description `caption` |

The three Wiki tables (`kb_wiki_pages` / `kb_wiki_folders` / `kb_wiki_jobs`) are defined in `core/db/models/kb_wiki.py`; see [Knowledge Base Wiki](./knowledge-base-wiki.md).

Child chunks never touch the relational DB — they are vectorized into the Milvus collection `hugagent_kb_private` (`core/kb/kb_vector.py`); every row carries `user_id` / `kb_id` for ownership isolation, and `row_type` distinguishes chunk rows, question rows, and image rows.

### Chunking and indexing

Parsing and chunking happen in `core/kb/kb_parser.py::parse_and_chunk()`, supporting five `chunk_method` values:

| Method | Best for |
|---|---|
| `semantic` (default) | General semantic segmentation |
| `qa` | Q&A-style documents |
| `laws` | Statutes / regulations (split by article) |
| `recursive` | Recursive fixed-size splitting |
| `embedding_semantic` | Semantic boundary detection via embedding similarity |

Parent-child parameters are tunable per upload via `indexing_config`: `parent_chunk_size` (default 1024 tokens), `child_chunk_size` (128), `overlap_tokens` (20), `parent_child_indexing` (default true). LLM enrichment is opt-in: `auto_keywords_count` (keywords per parent chunk, stored as tags and fed into sparse retrieval) and `auto_questions_count` (generated questions per parent chunk, indexed as separate question rows in Milvus to boost question-style recall). The background vectorization task is `core/content/kb_processing.py::vectorise_document_background()`.

### Multimodal: image retrieval

Figures inside documents are no longer discarded. Indexing runs through
`core/kb/kb_assets.py`:

1. **Extraction** — for PDFs, the external parse service's layout pass returns the figure
   bytes along with its caption, page index and bbox; a standalone image upload is itself
   one asset (it used to be force-decoded as plain text into garbage paragraphs — fixed).
   Bytes go to object storage, and the dead links in the text that pointed at the parse
   service's temp paths are rewritten to real asset URLs.
2. **Image understanding** — reuses the
   [vision bridge](./model-providers.md#vision-bridge-giving-text-only-models-eyes)
   (`core/vision`): the same path and the same `vision` model role that lets a text-only
   chat model read images. Three things come for free with it — an **evidence cache keyed
   by image content** (re-indexing the same document costs nothing), three provider
   protocols with structured-output degradation, and the hard constraint that only a
   multimodal provider can be assigned to that role. Mapping: `summary` → `caption`,
   `ocr.full_text` merges into `text_content` (alongside the document's own caption), and
   entities / relations / uncertainty are kept in `metadata.vision` for the ontology layer
   and human review. **Leaving `vision` unassigned (with a text-only main model) is a
   normal state**: images stay retrievable through their caption text, and configuring the
   role later backfills descriptions without re-parsing any document.
3. **Indexing** — each asset becomes one `row_type="image"` row whose `parent_chunk_id`
   points at the chunk containing it, so dedup, parent-text fetch, reranking and
   per-document deletion all keep working unchanged.
4. **Return** — a hit carries `images: [{asset_id, url, caption}]` for the model to answer
   from and the UI to render. Bytes are served by
   `GET /v1/catalog/kb/assets/{asset_id}`, whose visibility follows the owning KB space.

Toggles: `indexing_config.multimodal_indexing` (per space) takes precedence over the global
`KB_MULTIMODAL_INDEXING`. Per-image size caps, call timeouts and evidence-cache TTL belong
to the vision bridge (`VISION_*`); the knowledge base only throttles **batches**:
`KB_ASSET_CAPTION_BATCH` limits how many images are handed to the bridge at once — its
semaphore is process-wide, so pushing a hundred-figure document through in one go would
queue every interactive image read behind it. See the
[environment variable reference](../deployment/environment-variables.md).

Indexing runs in a `BackgroundTasks` worker thread (no running event loop) while the bridge
is async, so `anyio.from_thread` hands the coroutine back to the **host loop** (Starlette's
`run_in_threadpool` is `anyio.to_thread.run_sync`), which is what makes the evidence cache
and the semaphore actually apply. Only when no host loop is reachable (CLI / scripts) does it
spin up a temporary loop with the cache disabled — the Redis pool is a module-level global
bound to whichever loop first touches it, and letting a throwaway loop create it would strand
the whole process's Redis on a destroyed loop.

Audio and video reuse this same path: `kb_assets.kind` already widens to `audio` /
`video`, so wiring them up only means writing the ASR transcript into `text_content` —
indexing and retrieval downstream are unchanged.

### Retrieval path

`mcp_servers/retrieve_dataset_content_mcp/impl.py::retrieve_local_kb`:

1. Resolve the allowed `kb_id` set (environment variables in stdio mode; `x-allowed-kb-ids` and friends as HTTP headers in streamable-http mode);
2. `embed_text(query)` produces the query vector (embedding config reuses `MEM0_EMBED_*` or the DB `embedding` model role);
3. `core/kb/kb_vector.py::hybrid_search()`: two `AnnSearchRequest`s — dense vectors (IP metric) and sparse vectors (bag-of-words hashing into a 100k-dimension space) — fused with `RRFRanker(k=60)`; private spaces are filtered by `user_id == current user`, while EE public spaces pass only after their authorized `kb_id` is resolved;
4. Child-chunk / question-row / image-row hits are deduplicated, then the **parent chunk text** is fetched from PostgreSQL `kb_chunks` and returned to the LLM along with that chunk's image references;
5. If the user enabled reranking, results get a second pass through the reranker API (`RERANKER_URL/MODEL/API_KEY` or the DB `reranker` role).

Returned content follows the `[ref:retrieve_local_kb-N]` citation-marker convention, integrating with the citation system (see the [chat module](./chat.md)).

### API routes

User-facing routes are prefixed `/v1/catalog/kb` (`src/backend/api/routes/v1/kb.py`, request models in `kb_models.py`):

| Method | Path | Description |
|---|---|---|
| POST | `/v1/catalog/kb` | Create a KB space |
| PATCH / DELETE | `/v1/catalog/kb/{kb_id}` | Update / delete a space |
| POST | `/v1/catalog/kb/preview-chunks` | Preview chunking before upload |
| POST | `/v1/catalog/kb/polish-description` | AI-generated KB description |
| POST | `/v1/catalog/kb/{kb_id}/documents` | Upload a document (100MB cap, indexed in background) |
| GET | `/v1/catalog/kb/{kb_id}/documents[/{id}]` | Document list / detail |
| POST | `/v1/catalog/kb/{kb_id}/documents/{id}/reindex` | Re-index |
| POST | `/v1/catalog/kb/{kb_id}/wiki/rebuild` | Backfill the Wiki for existing documents (edit permission) |
| GET | `/v1/catalog/kb/{kb_id}/wiki/*` | Wiki read surface, see [Knowledge Base Wiki](./knowledge-base-wiki.md) |
| GET / PATCH | `/v1/catalog/kb/{kb_id}/chunks[/{chunk_id}]` | Chunk list / edit tags and questions |
| GET | `/v1/catalog/kb/assets/{asset_id}` | Read a media asset's raw bytes (visibility follows the owning space) |

Business logic is centralized in `core/services/kb_service.py::KBService`.

### System-managed space: MySpace sync

`KBService` maintains a special space, the "MySpace sync knowledge base" (`system_managed=true`, pinned, not editable / deletable / manually uploadable): once the user flips the sync switch, documents and images in MySpace (including AI conversation outputs) are automatically indexed, and new additions keep syncing. Entry points: `POST /v1/artifacts/{artifact_id}/knowledge-base` (manually add to any space) and `KBService.sync_artifact_to_my_space_kb()` (automatic sync). See [Projects & MySpace](./projects-myspace.md).

### Admin public knowledge bases (Enterprise Edition, EE)

The `/v1/admin/kb/*` admin routes live in `src/backend/api/routes/v1/admin_kb.py`, gated by the `content_admin` feature flag (EE router table in `api/routes/v1/__init__.py`). Public spaces are owned by the synthetic system account `system_public_kb` (`kb_service.py::SYSTEM_KB_OWNER_ID`), have `visibility=public`, and are visible and searchable by all users. The admin side additionally offers raw file download, Office-to-PDF online preview, and chunk content editing / deletion.

## External Dify knowledge bases (Enterprise Edition, EE)

The EE-only client is `src/backend/edition_ee/kb/dify.py`; shared routes call it through `core/kb/external_provider.py`. The derived CE tree replaces that seam with a disabled implementation and contains no Dify client. The `is_dify_enabled()` decision has three priority levels:

1. DB system config `knowledge_base.provider == "dify"` (editable in the Config console);
2. Environment variable `KNOWLEDGE_BASE=dify`;
3. Fallback: both `DIFY_URL` and `DIFY_API_KEY` are present.

When enabled, `api/routes/v1/catalog.py` injects Dify datasets into the `/v1/catalog` response as `kb` items at request time (60-second in-process cache), marked `visibility=public`. Retrieval goes through the MCP tool `retrieve_dataset_content`: without an explicit `dataset_id`, it searches all allowed datasets, supports Dify retrieval parameters such as `hybrid_search`, sorts merged results by score, truncates to top-k, and applies a token-budget trim.

```bash
KNOWLEDGE_BASE=dify
DIFY_URL=https://your-dify-host/v1     # alias: DIFY_BASE_URL
DIFY_API_KEY=dataset-...               # alias: DIFY_AUTH_TOKEN
```

## Supported file formats

KB upload validation is in `core/content/file_validation.py::validate_kb_file` (extension + magic-bytes double check), allowing: `.pdf` `.txt` `.md` `.doc` `.docx` `.xls` `.xlsx` `.csv` `.json`, plus images (`.png` `.jpg` `.jpeg` `.webp` `.gif`).

The general-purpose parser `core/content/file_parser.py::parse_file()` (shared by chat attachments and MySpace files) covers a wider range: PDF, DOCX, DOC/WPS (via LibreOffice conversion), TXT, XLSX/XLS, CSV, PPTX, plus plain-text formats (HTML / Markdown / JSON / YAML / source code, etc.) decoded directly as UTF-8.

## Frontend

- KB browsing and toggling is integrated into the capability center catalog (`src/frontend/src/components/catalog/`, state in `stores/catalogStore.ts`); CE shows one private module, while EE shows public and private modules;
- Creation / re-index modals: `src/frontend/src/components/kb/CreateKBModal.tsx`, `ReindexModal.tsx`;
- Media thumbnails `components/kb/KBAssetThumbs.tsx`: shared by the chunk viewer and the
  retrieval / citation cards in chat, click to enlarge. Retrieval hits use the `images`
  array they carry (captions included); older hits fall back to the asset links embedded
  in the chunk text;
- The admin public-KB console UI lives under `src/frontend/src/components/admin/` (Enterprise Edition, EE).

## Source map

| Path | Responsibility |
|---|---|
| `src/backend/core/kb/kb_parser.py` | Document parsing + parent-child chunking (5 chunk methods) |
| `src/backend/core/kb/kb_vector.py` | Milvus collection, embedding, hybrid search, reranking |
| `src/backend/core/kb/kb_assets.py` | Media assets: storage, image understanding (via the vision bridge), retrieval rows, result attachment |
| `src/backend/edition_ee/kb/dify.py` | Dify datasets client and enablement logic (EE only) |
| `src/backend/core/kb/external_provider.py` | Edition-neutral external-provider seam; disabled by the CE overlay |
| `src/backend/core/content/kb_processing.py` | Background vectorization, LLM keyword / question enrichment |
| `src/backend/core/content/file_validation.py` | Upload validation (extension + magic bytes) |
| `src/backend/core/content/file_parser.py` | General-purpose file parser |
| `src/backend/core/services/kb_service.py` | KB business logic (incl. the system-managed sync space) |
| `src/backend/api/routes/v1/kb.py` + `kb_models.py` | User-facing `/v1/catalog/kb` routes |
| `src/backend/api/routes/v1/admin_kb.py` | Admin public KB routes (Enterprise Edition, EE) |
| `src/backend/api/routes/v1/catalog.py` | Catalog aggregation (private only in CE; Dify / public spaces added in EE) |
| `src/backend/mcp_servers/retrieve_dataset_content_mcp/` | Retrieval MCP server (both tools) |
| `src/backend/core/db/models/knowledge.py` | `KBSpace` / `KBDocument` / `KBChunk` / `KBAsset` ORM |
| `src/frontend/src/components/kb/` | Creation / re-index modal components |

Related docs: [MCP Tools](./mcp-tools.md) · [Catalog](./catalog.md) · [Object Storage](./storage.md) · [Environment Variables](../deployment/environment-variables.md)
