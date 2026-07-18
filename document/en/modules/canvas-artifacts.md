# Canvas & Artifacts

> Last updated: 2026-06-11

Files produced during conversations (AI-generated reports, charts, spreadsheets, code-execution outputs) are unified in HugAgentOS under the **Artifact** abstraction: the backend provides persistent storage with a permission-checked download channel, while the frontend offers two viewing / editing surfaces (the data canvas and My Space), plus one-click public share links. This page covers the whole pipeline: how artifacts are produced, where they are stored, where to view them, how to edit them, and how to share them.

Personal canvas editing, the artifact center, and chat sharing are all **Community Edition (CE)**; **real-time multi-user canvas collaboration** is Enterprise Edition (license feature `canvas_collab`), and cloud storage (S3 / OSS) is Enterprise Edition (`cloud_storage`).

## Data canvas

The data canvas is a slide-out panel on the right of the chat UI (`src/frontend/src/components/canvas/CanvasPanel.tsx`), opened by clicking an attachment card in chat (`components/chat/ArtifactCardList.tsx`) or a file in My Space (`components/myspace/MySpacePanel.tsx`). The panel picks a renderer per file type:

| File type | Rendering | Editable |
|---|---|---|
| `.xlsx` / `.xls` | **Univer online spreadsheet** (see below) | ✅ cell / formula editing with save-back |
| `.docx` / `.doc` | docx preview rendering | read-only |
| `.pdf` | embedded PDF preview | read-only |
| `.pptx` / `.ppt` | converted preview | read-only |
| Images | direct display | read-only |
| Text / code (txt, md, csv, json, py, …) | text rendering | read-only |
| `.html` | HTML preview | read-only |

Panel state is managed by `stores/canvasStore.ts` (`openCanvas` / `closeCanvas` / `updateArtifact`; `openSeq` distinguishes "new file opened" from "same file refreshed after save").

### Univer spreadsheet integration

`components/canvas/UniverSpreadsheet.tsx` handles online xlsx editing:

1. The file is parsed with SheetJS (the `xlsx` package) and converted into Univer's `IWorkbookData` format (multiple sheets; number / boolean / formula cells; merged ranges; formula references recomputed by `utils/xlsxRange.ts::recomputeSheetRefs`).
2. At runtime it dynamically `import('@univerjs/presets')` plus `@univerjs/preset-sheets-core` (with the zh-CN locale) to render the spreadsheet — **only the free core preset is actually loaded**.
3. After editing, `exportXlsx()` produces a new xlsx File which `CanvasPanel` writes back to the same `file_id` via `api.ts::overwriteFile`; a dirty flag drives the Save button.

> Real-time collaborative editing is an Enterprise Edition capability (`Feature.CANVAS_COLLAB`, `core/licensing/features.py`). Note that `src/frontend/package.json` still declares the `@univerjs/preset-sheets-advanced` dependency (Univer's commercially licensed preset) — runtime code never imports it, and per the open-sourcing plan the CE-derived tree must not ship it.

## Artifact center (My Space)

### How artifacts are produced and persisted

During a streamed turn, files produced by tools first enter the per-conversation **workspace** (`core/llm/workspace`); only files that are explicitly **pinned** are written to the `artifacts` table at end of turn by `core/services/artifact_service.py::persist_artifacts` — this strict workspace gate keeps temporary intermediates from polluting My Space.

Physical storage is managed by `core/artifacts/store.py`, dual-mode by `STORAGE_TYPE`:

- `local` (default, Community Edition): files are written under `${STORAGE_PATH:-result}/artifacts/` with a local JSON index (`index.json`), served via FileResponse.
- `oss` (Enterprise Edition, `cloud_storage`): files are uploaded to Aliyun OSS; the local index is also backed up to OSS so it survives container restarts. The S3 backend lives in `core/storage/`.

Downloads all go through `GET /files/{file_id}` (`api/routes/files.py`), with an owner ∪ team permission check via `core/auth/permissions_iface.py::resolve_artifact_access`; Office files support PDF-converted online preview.

### REST API (`/v1/artifacts`, `api/routes/v1/artifacts.py`)

| Endpoint | Description |
|---|---|
| `GET ""` | File / image listing: filter by `type` (document/image), `source_kind` (user_upload/ai_generated), keyword, `scope` (personal/all incl. team), and personal `folder_id`; paginated |
| `GET /favorites` | Favorited chat sessions (the `ChatSession.favorite` flag, with last-message preview) |
| `POST /{artifact_id}/knowledge-base` | Add an artifact to a knowledge base in one click (background vectorization, see [Knowledge Base](knowledge-base.md)) |
| `DELETE /{artifact_id}` | Soft delete |

The frontend My Space panel lives in `src/frontend/src/components/myspace/` (`MySpacePanel.tsx` file listing + `FavoriteList.tsx` favorites), with state in `stores/mySpaceStore.ts`; personal folders are managed by `api/routes/v1/myspace_folders.py` — see [Projects & My Space](projects-myspace.md).

## Chat sharing

`api/routes/v1/chat_shares.py` provides read-only chat share links:

| Endpoint | Description |
|---|---|
| `POST /v1/chat-shares` | Create a share from a snapshot of chat messages (including plan-card data); expiry `3d` / `15d` / `3m` / `permanent` |
| `GET /v1/chat-shares/{share_id}` | Public read of the shared content (no login required) |
| `GET /v1/chat-shares` | My share records |
| `POST /{share_id}/revoke` / `restore` | Suspend / restore access |
| `DELETE /{share_id}` | Delete the record |

Shared content is a message **snapshot** stored in Redis (three `chat_share:*` key groups), degrading to in-process dicts when Redis is unavailable. The viewer is a standalone lightweight page, `src/frontend/src/SharePreviewApp.tsx` — `main.tsx` renders the share view instead of the main app whenever the URL carries a `?share=<share_id>` parameter. The share-record management page is `src/frontend/src/components/share/ShareRecordsPage.tsx`.

## How chart generation lands as artifacts

The MCP tool `generate_chart_tool` (`src/backend/mcp_servers/generate_chart_tool_mcp/`) renders charts with matplotlib from data + instructions (line / bar / pie, etc.). The generated PNG is persisted directly as an artifact via `core/artifacts/store.py::save_artifact_bytes`, returning:

```json
{"ok": true, "file_id": "<artifact id>", "url": "/files/<file_id>",
 "name": "chart_xxx.png", "mime_type": "image/png"}
```

The chart immediately appears in the chat attachment area, previewable in the canvas and collectible in My Space. The tool description enforces "fetch data first, then plot" — fabricating numbers is forbidden. To embed a chart into a Word/PPT being built inside the sandbox, the `file_id` must first be copied into the sandbox with `sandbox_put_artifact`, then referenced by its sandbox path from the CLI — artifact storage and the sandbox filesystem are separate worlds.

## How report export lands as artifacts

The MCP tool group `report_export_mcp` (`src/backend/mcp_servers/report_export_mcp/`) does lightweight Markdown → Office export, persisted through `save_artifact_bytes` as well:

| Tool | Description |
|---|---|
| `export_report_to_docx` | Markdown report → .docx (official-document fonts; based on the `reference.docx` template). **Marked DEPRECATED** — for complex layout (custom styles, headers/footers, TOC, image insertion, template fill) use the word-editing skill `word-cli create` instead |
| `export_table_to_excel` | Parses Markdown tables → .xlsx download |

The returned `file_id` / `url` matches the chart tool's contract and shows up automatically in the attachment area; the xlsx can then be edited further right in the data canvas.

## End-to-end example

> User: "Look up the last five years of EV production volume, plot it as a bar chart, then export the analysis as Word."

1. The agent fetches data via search / data tools;
2. calls `generate_chart_tool` → artifact A (PNG, visible in attachments, previewable in canvas);
3. writes the analysis and calls `export_report_to_docx` → artifact B (docx);
4. both artifacts are pinned into the workspace with the `meta` event and persisted by `persist_artifacts`, appearing in My Space;
5. the user previews the docx in the canvas, then `POST /v1/chat-shares` creates a 15-day share link to send to a colleague.

## Source map

| Topic | Path |
|---|---|
| Canvas panel / Univer spreadsheet | `src/frontend/src/components/canvas/CanvasPanel.tsx`, `UniverSpreadsheet.tsx`, `src/frontend/src/stores/canvasStore.ts` |
| Artifact REST API | `src/backend/api/routes/v1/artifacts.py` |
| Artifact store (local/OSS) | `src/backend/core/artifacts/store.py` |
| Artifact persistence (workspace gate) | `src/backend/core/services/artifact_service.py` |
| File download / preview | `src/backend/api/routes/files.py` |
| Chat share API | `src/backend/api/routes/v1/chat_shares.py` |
| Share frontend | `src/frontend/src/SharePreviewApp.tsx`, `src/frontend/src/components/share/ShareRecordsPage.tsx` |
| Chart generation MCP | `src/backend/mcp_servers/generate_chart_tool_mcp/server.py`, `chart.py` |
| Report export MCP | `src/backend/mcp_servers/report_export_mcp/server.py`, `impl.py` |
| My Space frontend | `src/frontend/src/components/myspace/`, `src/frontend/src/stores/mySpaceStore.ts` |

Further reading: [Sandbox](sandbox.md) · [Projects & My Space](projects-myspace.md) · [Storage](storage.md) · [MCP Tools](mcp-tools.md)
