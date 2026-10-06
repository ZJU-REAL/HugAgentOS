import type { AgentItem, CatalogItemBase, MCPItem, SkillItem } from './capabilities';

export interface KBDocument {
  id: string;
  title: string;
  desc?: string;
  content?: string;
  indexing_status?: string;  // normalized local/external indexing status
  word_count?: number;
  size_bytes?: number;
  created_at?: number;
}

export interface KBChunk {
  chunk_id: string;
  document_id: string;
  chunk_index: number;
  content: string;
  tags: string[];
  questions: string[];
}

/** 索引模式：rag 建向量检索，wiki 建 LLM Wiki 图谱；两者同选即 LLM-Wiki 知识库。 */
export type KBIndexMode = 'rag' | 'wiki';

/** 知识库能力位。自建库由索引模式投影而来，外接库由后端上报，两者字段同构。 */
export interface KBCapabilities {
  vector?: boolean;
  keyword?: boolean;
  wiki?: boolean;
  graph?: boolean;
}

/** Wiki 抽取粒度：条目数量与生成成本随档位单调上升。 */
export type WikiGranularity = 'focused' | 'standard' | 'exhaustive';

export interface WikiConfig {
  granularity?: WikiGranularity;
  language?: string;
  max_llm_calls?: number;
  /** 抽取要求：这个库里什么算重要的条目。只影响抽什么，出处与接地规则由系统保证 */
  extraction_instructions?: string;
  /** 撰写要求：条目页的语气与结构。只影响怎么写，出处与接地规则由系统保证 */
  content_instructions?: string;
}

export interface KBItem extends CatalogItemBase {
  provider?: string;
  version?: string;
  inputs?: string;
  outputs?: string;
  documents?: KBDocument[];
  visibility?: 'public' | 'private';
  is_public?: boolean;
  document_count?: number;
  chunk_method?: string;
  /** 知识库来源：自建库还是外接后端。由后端显式标注，前端不要靠 id 前缀推断 */
  source?: 'local' | 'external';
  index_modes?: KBIndexMode[];
  capabilities?: KBCapabilities;
  system_managed?: boolean;
  pinned?: boolean;
  editable?: boolean;
  deletable?: boolean;
  uploadable?: boolean;
}

/** 单个知识库的 Wiki 状态：能力位 + 生成进度。 */
export interface KBWikiStatus {
  kb_id: string;
  supports_wiki: boolean;
  generating?: boolean;
  running?: number;
  pending?: number;
  failed?: number;
  progress?: { stage?: string; done?: number; total?: number };
  last_error?: string | null;
}

export interface ChunkPreviewChild {
  index: number;
  content: string;
}

export interface ChunkPreviewItem {
  index: number;
  content: string;
  token_count: number;
  children_count: number;
  children_preview: ChunkPreviewChild[];
}

export interface ChunkPreviewResult {
  total_chunks: number;
  total_children: number;
  chunks: ChunkPreviewItem[];
}

export interface MemoryItem {
  id: string;
  memory: string;
  created_at?: string;
  updated_at?: string;
  score?: number;
  // metadata fields newly flattened by the backend (may be missing; kept compatible with old data)
  layer?: 'L1' | 'L2' | 'L3' | 'session';
  source?: string;
  tags?: string[];
  confidentiality?: 'public' | 'internal' | 'sensitive';
  ttl_days?: number;
  evidence?: string;
  /** L2 stores procedures only; these carry the rule's reason and its scope. */
  memory_type?: string;
  why?: string;
  applies_to?: string;
  strength?: string;
}

export interface MemoryProfile {
  enabled: boolean;
  workspace_id: string;
  content_md: string;
  length: number;
  max_chars: number;
}

export interface MemoryGraphRelation {
  source: string;
  relationship: string;
  target: string;
}

export interface Catalog {
  skills: SkillItem[];
  agents: AgentItem[];
  mcp: MCPItem[];
  kb: KBItem[];
}

// ── 知识库的 LLM Wiki / 概念图谱（自建库与具备该能力的外接后端共用） ──────────

export type WikiPageType =
  | 'concept'
  | 'entity'
  | 'synthesis'
  | 'comparison'
  | 'summary'
  | 'index';

/** 「知识」Tab 合并的页面类型；「摘要」自成一个 Tab，「索引」是虚拟总览视图 */
export const WIKI_KNOWLEDGE_TYPES = ['entity', 'concept', 'synthesis', 'comparison'] as const;

/** Wiki 目录树的一个节点（page_count 是递归计数，has_children 决定是否给展开箭头） */
export interface WikiFolder {
  id: string;
  name: string;
  parent_id?: string;
  page_count: number;
  has_children: boolean;
}

export interface WikiIndexItem {
  slug: string;
  title: string;
  summary?: string;
  wiki_path?: string;
}

export interface WikiIndexGroup {
  type: string;
  type_label?: string;
  total: number;
  items: WikiIndexItem[];
}

/** 索引总览：一段总述 + 按类型分组的条目 */
export interface WikiIndexOverview {
  intro: string;
  version?: number;
  groups: WikiIndexGroup[];
}

/** 定位阶段的精简页面：只带判断相关性所需的信息，不含长正文 */
export interface WikiPageBrief {
  slug: string;
  title: string;
  page_type: WikiPageType | string;
  type_label?: string;
  summary?: string;
  aliases?: string[];
  category_path?: string[];
  wiki_path?: string;
  out_links?: string[];
  in_links?: string[];
  source_refs?: string[];
  chunk_refs?: string[];
  updated_at?: string;
}

/** 相关概念：后端已把 out_links 的 slug 解析成可读标题 */
export interface WikiRelatedPage {
  slug: string;
  title: string;
  page_type: string;
}

/** 读页返回：在精简页基础上补上正文 markdown 与解析过的相关概念 */
export interface WikiPageDetail extends WikiPageBrief {
  content: string;
  related_pages?: WikiRelatedPage[];
}

export interface WikiStats {
  total_pages: number;
  pages_by_type: Record<string, number>;
  total_links: number;
  orphan_count: number;
}

export interface WikiGraphNode {
  slug: string;
  title: string;
  page_type: string;
  link_count?: number;
}

export interface WikiGraphEdge {
  source: string;
  target: string;
  /** 边上的关系名；概念图谱不带，实体关系图谱（L3）用它画关系动词 */
  label?: string;
}

export interface WikiGraphData {
  nodes: WikiGraphNode[];
  edges: WikiGraphEdge[];
  meta?: {
    mode?: string;
    total?: number;
    returned?: number;
    /** 旧键，外接后端可能仍在返回；等价 total */
    total_pages?: number;
    truncated?: boolean;
    center?: string;
    depth?: number;
  };
}

/** 顺血缘取回的原文分块 */
export interface WikiSourceChunk {
  chunk_id: string;
  document_id: string;
  document_title?: string;
  content: string;
}

export interface WikiCapability {
  provider: string;
  supports_wiki: boolean;
}
