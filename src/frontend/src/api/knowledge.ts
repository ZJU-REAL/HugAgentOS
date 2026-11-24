import type { KBIndexMode, WikiConfig, KBWikiStatus, KBChunk, ChunkPreviewResult, WikiCapability, WikiStats, WikiPageBrief, WikiFolder, WikiIndexOverview, WikiPageDetail, WikiGraphData, WikiSourceChunk } from '../types';
import { apiRequest, getApiUrl, type KBDocumentsResponse, type KBDocumentStatusCounts, type PaginatedData, throwIfSessionExpired, unwrapData, uploadErrorMessage } from './core';

export interface KBDocumentItem {
  id: string;
  document_id?: string;
  title: string;
  name?: string;
  filename?: string;
  desc?: string;
  word_count?: number;
  size?: number;
  size_bytes?: number;
  indexing_status?: string;
  indexing_error?: string | null;
  enabled?: boolean;
  data_source_type?: string;
  /** 外接知识库（WeKnora）文档是否有可经 /original 取回的原文件 */
  original_available?: boolean;
  created_at?: number | string;
  uploaded_at?: number | string;
  createdAt?: number | string;
  uploadedAt?: number | string;
  content?: string;
}

export async function getKBDocuments(
  kbId: string,
  page = 1,
  pageSize = 20,
  keyword?: string,
): Promise<KBDocumentsResponse> {
  try {
    const kw = keyword?.trim() ? `&keyword=${encodeURIComponent(keyword.trim())}` : '';
    const wrapped = await apiRequest<unknown>(
      `/v1/catalog/kb/${kbId}/documents?page=${page}&page_size=${pageSize}${kw}`,
    );
    const data = unwrapData<PaginatedData<KBDocumentItem> & { status_counts?: KBDocumentStatusCounts }>(wrapped);
    const items = Array.isArray(data.items) ? data.items : [];
    const pagination = data.pagination;
    return {
      items,
      total: typeof pagination?.total_items === 'number' ? pagination.total_items : items.length,
      page: typeof pagination?.page === 'number' ? pagination.page : page,
      page_size: typeof pagination?.page_size === 'number' ? pagination.page_size : pageSize,
      has_more: Boolean(pagination?.has_next),
      status_counts: data.status_counts,
    };
  } catch {
    return {
      items: [],
      total: 0,
      page,
      page_size: pageSize,
      has_more: false,
    };
  }
}

/** 手机上的连续列表：一次取前 `count` 条（后端单页上限 100，超出时按 100 一页依次取再拼接）。
 *  返回的 page_size 按调用方的「每批条数」回填，调用方据此算总批数。 */
export async function getKBDocumentsWindow(
  kbId: string,
  count: number,
  batchSize: number,
  keyword?: string,
): Promise<KBDocumentsResponse> {
  const chunk = Math.min(100, count);
  const first = await getKBDocuments(kbId, 1, chunk, keyword);
  const items = [...first.items];
  let page = 1;
  let hasMore = first.has_more;
  while (items.length < count && hasMore) {
    page += 1;
    const next = await getKBDocuments(kbId, page, chunk, keyword);
    items.push(...next.items);
    hasMore = next.has_more;
  }
  return {
    ...first,
    items: items.slice(0, count),
    page: Math.ceil(count / batchSize),
    page_size: batchSize,
    has_more: first.total > count,
  };
}

export async function getKBDocumentDetail(
  kbId: string,
  _documentId: string,
): Promise<{ title: string; content: string; desc?: string }> {
  const wrapped = await apiRequest<unknown>(
    `/v1/catalog/kb/${kbId}/documents/${_documentId}`,
  );
  const data = unwrapData<{ title?: string; content?: string; desc?: string }>(wrapped);
  const rawTitle = typeof data.title === 'string' ? data.title.trim() : '';
  return {
    title: rawTitle,
    content: data.content || '',
    desc: data.desc,
  };
}

// ── Private knowledge base management API ──────────────────────────────────

export interface IndexingConfig {
  parent_chunk_size?: number;
  child_chunk_size?: number;
  overlap_tokens?: number;
  parent_child_indexing?: boolean;
  auto_keywords_count?: number;
  auto_questions_count?: number;
  // Parent-chunk separator hierarchy (only effective for recursive chunking and the recursive fallback of semantic chunking); empty uses the built-in defaults
  separators?: string[];
  // Child-chunk separator hierarchy (in parent-child chunking, child chunks are split by this, then packed by child_size); empty falls back to fixed-length sliding window
  child_separators?: string[];
}

export async function createKBSpace(
  name: string,
  description?: string,
  chunkMethod?: string,
  indexingConfig?: IndexingConfig,
  visibility?: 'private' | 'public',
  indexModes?: KBIndexMode[],
  wikiConfig?: WikiConfig,
): Promise<Record<string, unknown>> {
  const wrapped = await apiRequest<unknown>('/v1/catalog/kb', {
    method: 'POST',
    body: JSON.stringify({
      name,
      description: description || undefined,
      chunk_method: chunkMethod || 'semantic',
      indexing_config: indexingConfig || undefined,
      visibility: visibility || 'private',
      index_modes: indexModes && indexModes.length ? indexModes : undefined,
      wiki_config: wikiConfig || undefined,
    }),
  });
  return unwrapData<Record<string, unknown>>(wrapped);
}

export async function updateKBSpace(
  kbId: string,
  payload: {
    name?: string;
    description?: string;
    index_modes?: KBIndexMode[];
    wiki_config?: WikiConfig;
  },
): Promise<Record<string, unknown>> {
  const wrapped = await apiRequest<unknown>(`/v1/catalog/kb/${kbId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
  return unwrapData<Record<string, unknown>>(wrapped);
}

/** 单库 Wiki 状态：是否具备 Wiki 能力 + 生成进度。无权访问时返回 supports_wiki:false。 */
export async function getKBWikiStatus(kbId: string): Promise<KBWikiStatus> {
  try {
    const wrapped = await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/capability`);
    return unwrapData<KBWikiStatus>(wrapped);
  } catch {
    return { kb_id: kbId, supports_wiki: false };
  }
}

/** 把该库全部已索引文档重新排进 Wiki 生成队列（建库后才勾 Wiki、或想换粒度重来）。 */
export async function rebuildKBWiki(kbId: string): Promise<{ job_id: string; documents: number }> {
  const wrapped = await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/rebuild`, {
    method: 'POST',
  });
  return unwrapData<{ job_id: string; documents: number }>(wrapped);
}

export async function polishKBDescription(
  name: string,
  description?: string,
): Promise<string> {
  const wrapped = await apiRequest<unknown>('/v1/catalog/kb/polish-description', {
    method: 'POST',
    body: JSON.stringify({
      name,
      description: description || undefined,
    }),
  });
  const data = unwrapData<{ description?: string }>(wrapped);
  return typeof data.description === 'string' ? data.description : '';
}

export async function uploadKBDocument(
  kbId: string,
  file: File,
  title?: string,
  indexingConfig?: IndexingConfig,
  chunkMethod?: string,
): Promise<Record<string, unknown>> {
  const url = `${getApiUrl()}/v1/catalog/kb/${kbId}/documents`;
  const formData = new FormData();
  formData.append('file', file);
  if (title) formData.append('title', title);
  if (indexingConfig) formData.append('indexing_config', JSON.stringify(indexingConfig));
  if (chunkMethod) formData.append('chunk_method', chunkMethod);

  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    body: formData,
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throwIfSessionExpired(response.status, payload);
    throw new Error(uploadErrorMessage(response.status, payload));
  }

  const payload = await response.json();
  return unwrapData<Record<string, unknown>>(payload);
}

export async function deleteKBSpace(kbId: string): Promise<void> {
  await apiRequest(`/v1/catalog/kb/${kbId}`, { method: 'DELETE' });
}

export async function deleteKBDocument(kbId: string, documentId: string): Promise<void> {
  await apiRequest(`/v1/catalog/kb/${kbId}/documents/${documentId}`, { method: 'DELETE' });
}

export async function getKBChunks(
  kbId: string,
  docId: string,
  page = 1,
  pageSize = 100,
): Promise<KBChunk[]> {
  const wrapped = await apiRequest<unknown>(
    `/v1/catalog/kb/${encodeURIComponent(kbId)}/chunks?document_id=${encodeURIComponent(docId)}&page=${page}&page_size=${pageSize}`,
  );
  const data = unwrapData<{ items?: KBChunk[] }>(wrapped);
  return Array.isArray(data.items) ? data.items : [];
}

export interface KBChunkChild {
  chunk_id: string;
  chunk_index: number;
  content: string;
}

/** Fetch a parent chunk's child chunks from the vector store (parent-child chunking mode; flat mode returns an empty array). */
export async function getKBChunkChildren(
  kbId: string,
  chunkId: string,
): Promise<KBChunkChild[]> {
  const wrapped = await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/chunks/${chunkId}/children`);
  const data = unwrapData<{ children?: KBChunkChild[] }>(wrapped);
  return Array.isArray(data.children) ? data.children : [];
}

export async function updateKBChunk(
  kbId: string,
  chunkId: string,
  data: { content?: string; tags?: string[]; questions?: string[] },
): Promise<void> {
  await apiRequest(`/v1/catalog/kb/${kbId}/chunks/${chunkId}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

export async function reindexKBDocument(
  kbId: string,
  docId: string,
  indexingConfig?: IndexingConfig,
  chunkMethod?: string,
): Promise<void> {
  const body: Record<string, unknown> = {};
  if (indexingConfig) body.indexing_config = { ...indexingConfig };
  if (chunkMethod) body.chunk_method = chunkMethod;
  await apiRequest(`/v1/catalog/kb/${kbId}/documents/${docId}/reindex`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ── Chunk preview API ───────────────────────────────────────────

export async function previewChunks(
  file: File,
  chunkMethod = 'structured',
  parentChunkSize = 1024,
  childChunkSize = 128,
  overlapTokens = 20,
  parentChildIndexing = true,
  separators?: string[],
  childSeparators?: string[],
): Promise<ChunkPreviewResult> {
  const url = `${getApiUrl()}/v1/catalog/kb/preview-chunks`;
  const formData = new FormData();
  formData.append('file', file);
  formData.append('chunk_method', chunkMethod);
  formData.append('parent_chunk_size', String(parentChunkSize));
  formData.append('child_chunk_size', String(childChunkSize));
  formData.append('overlap_tokens', String(overlapTokens));
  formData.append('parent_child_indexing', String(parentChildIndexing));
  if (separators && separators.length) formData.append('separators', JSON.stringify(separators));
  if (childSeparators && childSeparators.length) formData.append('child_separators', JSON.stringify(childSeparators));

  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    body: formData,
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throwIfSessionExpired(response.status, payload);
    throw new Error(uploadErrorMessage(response.status, payload));
  }

  const payload = await response.json();
  return unwrapData<ChunkPreviewResult>(payload);
}

// ── 知识库的 LLM Wiki / 概念图谱 ─────────────────────────────────────────────
//
// 两类知识库都可能有这层结构化产物：勾选了 Wiki 索引模式的自建库，以及提供该
// 能力的外接后端。前端先调 getWikiCapability 做平台级探测，再看单个知识库的
// capabilities.wiki——两级都为真才渲染入口，避免出现注定 404 的按钮。

export async function getWikiCapability(): Promise<WikiCapability> {
  try {
    return unwrapData<WikiCapability>(
      await apiRequest<unknown>('/v1/catalog/kb/wiki/capability'),
    );
  } catch {
    return { provider: '', supports_wiki: false };
  }
}

export async function getWikiStats(kbId: string): Promise<WikiStats> {
  return unwrapData<WikiStats>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/stats`),
  );
}

export async function getWikiPages(
  kbId: string,
  options: {
    page?: number;
    pageSize?: number;
    /** 逗号分隔多类型，如 entity,concept,synthesis,comparison */
    pageType?: string;
    /** 把范围收窄到某个目录节点 */
    categoryPath?: string;
    categoryDepth?: number;
  } = {},
): Promise<{ pages: WikiPageBrief[]; total: number }> {
  const { page = 1, pageSize = 50, pageType = '', categoryPath = '', categoryDepth = 0 } = options;
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (pageType) params.set('page_type', pageType);
  if (categoryPath) {
    params.set('category_path', categoryPath);
    params.set('category_depth', String(categoryDepth || 1));
  }
  const data = unwrapData<{ pages?: WikiPageBrief[]; total?: number }>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/pages?${params}`),
  );
  return { pages: data.pages || [], total: data.total || 0 };
}

/** 目录树的某一层；parentId 留空取根层 */
export async function getWikiFolders(
  kbId: string,
  parentId = '',
  pageTypes = '',
): Promise<WikiFolder[]> {
  const params = new URLSearchParams();
  if (parentId) params.set('parent_id', parentId);
  if (pageTypes) params.set('page_types', pageTypes);
  const qs = params.toString();
  const data = unwrapData<{ folders?: WikiFolder[] }>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/folders${qs ? `?${qs}` : ''}`),
  );
  return data.folders || [];
}

export async function getWikiIndexOverview(
  kbId: string,
  limit = 20,
): Promise<WikiIndexOverview> {
  const data = unwrapData<WikiIndexOverview>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/index?limit=${limit}`),
  );
  return { intro: data.intro || '', version: data.version, groups: data.groups || [] };
}

export async function searchWikiPages(
  kbId: string,
  query: string,
  limit = 20,
): Promise<{ pages: WikiPageBrief[]; total: number }> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  const data = unwrapData<{ pages?: WikiPageBrief[]; total?: number }>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/search?${params}`),
  );
  return { pages: data.pages || [], total: data.total || 0 };
}

export async function getWikiPage(kbId: string, slug: string): Promise<WikiPageDetail> {
  return unwrapData<WikiPageDetail>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/page/${slug}`),
  );
}

export async function getWikiGraph(
  kbId: string,
  options: { mode?: 'overview' | 'ego'; center?: string; depth?: number; limit?: number; types?: string } = {},
): Promise<WikiGraphData> {
  const { mode = 'overview', center = '', depth = 1, limit = 60, types = '' } = options;
  const params = new URLSearchParams({ mode, limit: String(limit) });
  if (mode === 'ego') {
    params.set('center', center);
    params.set('depth', String(depth));
  }
  if (types) params.set('types', types);
  const data = unwrapData<WikiGraphData>(
    await apiRequest<unknown>(`/v1/catalog/kb/${kbId}/wiki/graph?${params}`),
  );
  return { nodes: data.nodes || [], edges: data.edges || [], meta: data.meta };
}

export async function getWikiSourceChunks(
  kbId: string,
  slug: string,
  maxChunks = 6,
): Promise<{ page: WikiPageDetail; chunks: WikiSourceChunk[] }> {
  const data = unwrapData<{ page?: WikiPageDetail; chunks?: WikiSourceChunk[] }>(
    await apiRequest<unknown>(
      `/v1/catalog/kb/${kbId}/wiki/source/${slug}?max_chunks=${maxChunks}`,
    ),
  );
  return { page: data.page as WikiPageDetail, chunks: data.chunks || [] };
}
