# Knowledge Base Wiki

After uploading a pile of scattered documents, you often still have no idea what is actually in them. That is what the Wiki is for: once documents are indexed, the system uses an LLM to extract people, organisations, concepts and other items from the source text, generates a **sourced** Markdown page for each item, and links those pages to one another — producing an encyclopedia-style site you can browse.

The difference from plain Q&A: Q&A answers what you ask; the Wiki organises the knowledge for you up front. The more scattered the material, the more the Wiki pays off.

> **The Wiki is a map of the knowledge base, not a second retrieval source.** The map
> narrows the scope to the right place; answers and citations always come back to the
> original chunks — every page can pull its source text by ID rather than passing off a
> Wiki summary as the answer.

Both self-hosted knowledge bases and external backends that support this capability expose the same structure, with identical read APIs and UI.

## Enabling it

1. When creating a knowledge base, tick **Wiki graph** under "Index mode" (tick it together with "RAG retrieval" for an LLM-Wiki knowledge base);
2. Upload documents;
3. Wait. Generation is asynchronous and takes a while with many documents; the "Knowledge Wiki" tab on the detail page shows progress;
4. Browse under the "Knowledge Wiki" tab, switching between the directory tree and the concept graph.

To add the Wiki to an existing knowledge base: edit it, tick "Wiki graph", save, then click **Generate Wiki**. Changing the setting does **not** backfill automatically — that would spend model quota, so it requires an explicit action.

Generation calls an LLM, with cost proportional to document volume. Extraction density is chosen at creation time (see "Extraction granularity").

## Page model

### Page types

| Type | Description |
|---|---|
| `summary` | Summary page for one source document |
| `entity` | Entity page (person, organisation, product, place, technology, event) |
| `concept` | Concept page (topic, methodology, theory) |
| `index` | Whole-base index page |
| `synthesis` / `comparison` | Synthesis / comparison pages — not produced by the pipeline, reserved for agent authoring |

### Directory tree

Concept pages are organised into folders, at most 3 levels deep. Folders are planned by the model **for a whole batch at once** — one call assigns paths for all items and is required to reuse existing folders, so items of the same kind land together instead of each page inventing its own synonym folders.

Empty folders may exist on their own; the finalize stage prunes folders that have neither pages nor subfolders.

### Key fields

- `slug`: unique identifier within the base, formatted `<type>/<name>`, e.g. `entity/zhongguo-renmin-yinhang`;
- `source_refs`: which documents the page draws on; `chunk_refs`: which original chunks support the content;
- `in_links` / `out_links`: bidirectional links between pages, which the concept graph is built from;
- `aliases`: alternate names, used for search and automatic in-text linking.

## Generation pipeline

Documents are queued automatically once indexing completes, then processed asynchronously by a background worker in five stages:

| Stage | What it does |
|---|---|
| Candidate extraction | Extract a skeleton per document: name, slug, aliases, one-line description |
| Citation | Per batch of chunks, mark which chunks substantively discuss each candidate |
| Deduplication | Decide whether a new item refers to the same thing as an existing page, and merge if so |
| Taxonomy planning | Assign directory paths for the whole batch at once |
| Page authoring | Incrementally merge or create pages per item, then write a summary page per source document |
| Finalize | Rebuild the index page, strip dead links, rebuild backlinks, prune empty folders (no LLM calls) |

Three constraints run through the whole pipeline and account for the Wiki's trustworthiness:

1. **The model never paraphrases facts.** The citation stage only answers "which chunks discuss this"; facts reach the authoring stage as verbatim chunk text.
2. **When authoring, the model is a compiler, not a writer.** It stays close to the source wording — no stylistic rewriting, no expansion, no rhetorical filler. New information that clearly belongs to a related-but-different thing is rejected.
3. **Related ≠ same.** Deduplication only merges different names for the same thing (abbreviation, translation, long/short form). Different products, versions, or documents in the same category are never merged.

### Extraction granularity

| Granularity | Behaviour |
|---|---|
| `focused` | Only 3-7 core topics; cleanest index, lowest cost |
| `standard` (default) | Topics plus entities/concepts that get a dedicated block of content |
| `exhaustive` | Everything named; suits a glossary, but produces many items at higher cost |

### Generation model

Bind a provider to the **"Knowledge base Wiki extraction"** role (`kb_wiki`) in Model Management. Without a binding it falls back to the main chat model.

The Wiki is pure offline batch work whose call volume grows linearly with document count, so **binding a cheaper model is the normal setup**. Each run also has a call-count ceiling (`wiki_config.max_llm_calls`, default 4000) as a brake: on reaching it, whatever was generated is kept and the job ends normally.

### Failure and recovery

Job state is persisted, and the worker writes a heartbeat after claiming a job. On process restart, jobs with a stale heartbeat are reclaimed and re-run — you never end up with half a Wiki. Failed jobs retry with backoff and terminate with a recorded error once attempts are exhausted.

## Browsing and search

The UI lives under the "Knowledge Wiki" tab of the knowledge base detail page: a directory tree on the left, grouped by type and lazily loaded level by level; page content plus "Source" on the right; and a concept graph view showing how items link together.

Search supports alternation: `licence|permit|certificate` tries several wordings at once — colloquial phrasing often fails to match formal terminology.

### API

All endpoints sit under `/v1/catalog/kb/{kb_id}/wiki` and require read access to the knowledge base:

| Method | Path | Description |
|---|---|---|
| GET | `/capability` | Whether this base has a Wiki, plus generation progress |
| GET | `/stats` | Size statistics |
| GET | `/pages` | Paginated page list, filterable by type and folder |
| GET | `/page/{slug}` | Read one page (content, links, lineage) |
| GET | `/folders` | One level of the directory tree, with recursive page counts |
| GET | `/index` | Index overview |
| GET | `/search?q=` | Search pages |
| GET | `/graph` | Concept graph (`overview` global / `ego` centred on a node) |
| GET | `/source/{slug}` | Pull the original chunks behind a page |
| POST | `/rebuild` | Regenerate (requires edit permission) |

Platform-level probing uses `GET /v1/catalog/kb/wiki/capability`.

> **Permissions**: the Wiki is derived from the source text, so its readability matches the
> source exactly, using the same check as knowledge base retrieval. It is not a way around
> knowledge base authorisation.

## Agent integration

Once enabled, agents gain five tools forming a **one-way chain**:

| Tool | Purpose |
|---|---|
| `wiki_overview` | See the shape of this base's knowledge and its hub concepts |
| `wiki_locate` | ① Match concept/entity pages by keyword — summaries only, no body text |
| `wiki_read_page` | Read one page's full content and relationships |
| `wiki_expand` | ② Follow links to gather related concepts at once; suits aggregate questions |
| `wiki_fetch_source` | ③ Fetch original chunks by ID along the lineage — the basis for answers and citations |

The tools work for both self-hosted and external knowledge bases; just pass the relevant knowledge base ID.

## Lifecycle

| Event | Behaviour |
|---|---|
| Document uploaded | Queued for generation once indexing completes |
| Document deleted | Its lineage is removed; pages left with no source are archived |
| Knowledge base deleted | Wiki data is deleted along with it |
| Document reindexed | Chunk IDs change — delete and re-upload the document, or click "Generate Wiki" afterwards to rebuild |

## Source map

| Layer | Location |
|---|---|
| Data model | `src/backend/core/db/models/kb_wiki.py` |
| Pipeline | `src/backend/core/kb/wiki/` (`extract` / `cite` / `dedup` / `taxonomy` / `reduce` / `finalize` / `pipeline`) |
| Prompts | `src/backend/core/kb/wiki/prompts.py` |
| Jobs and worker | `src/backend/core/kb/wiki/jobs.py`, `worker.py` |
| Read surface | `src/backend/core/kb/wiki/local_provider.py` |
| Per-base dispatch | `src/backend/core/kb/wiki_router.py` |
| Routes | `src/backend/api/routes/v1/kb_wiki.py` |
| Agent tools | `src/backend/mcp_servers/retrieve_dataset_content_mcp/wiki_impl.py` |
| Frontend | `src/frontend/src/components/kb/` (`WikiPanel` / `WikiTree` / `ConceptGraph` / `IndexModePicker`) |
