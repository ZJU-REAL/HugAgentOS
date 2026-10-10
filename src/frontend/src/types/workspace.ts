import type { EditionChatShareScope, EditionProjectChatFields, EditionProjectFields, EditionProjectKind } from '../editionModelTypes';

export type PanelKey = 'chat' | 'skills' | 'agents' | 'mcp' | 'kb' | 'docs' | 'app_center' | 'settings' | 'my_space' | 'ability_center' | 'lab' | 'projects' | 'automation' | 'sites';

/** 能力中心的四类能力。选中项由侧边栏的二级导航驱动，所以状态放在 catalogStore 而非页面内部。 */
export type AbilityTabKey = 'agents' | 'skills' | 'mcp' | 'plugins';

// ─── Projects (Claude-style workspaces) ─────────────────────────────────────

export type ProjectKind = 'personal' | EditionProjectKind;

export type ProjectPermission = 'admin' | 'edit' | 'view' | 'none';

export type ProjectFileSource = 'upload' | 'reference';

export interface ProjectItem extends EditionProjectFields {
  project_id: string;
  name: string;
  description: string;
  kind: ProjectKind;
  owner_user_id: string;
  /** user_folder.folder_id a personal project is linked to */
  linked_folder_id: string | null;
  /** Linked folder name (for frontend display) */
  folder_name: string | null;
  instructions: string;
  instructions_source?: 'AGENTS.md' | 'legacy' | 'missing';
  instructions_revision?: string;
  icon_color: string | null;
  pinned: boolean;
  favorite: boolean;
  /** Project-level memory read switch (whether in-project sessions can retrieve / display project memories; default ON) */
  memory_enabled: boolean;
  /** Project-level memory write switch (whether project memories are extracted and written after an in-project session ends; default ON) */
  memory_write_enabled: boolean;
  permission: ProjectPermission;
  /** Project creator, or (team projects) the team owner: may delete and set visibility. */
  is_owner: boolean;
  file_count: number;
  chat_count: number;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  last_activity_at: string | null;
}

export interface ProjectDetail extends ProjectItem {
  capacity_used?: number;
  capacity_limit?: number;
}

/**
 * Project file = an artifact under the linked MySpace folder subtree.
 *
 * - ``id`` == ``artifact_id`` (there is no longer a separate project_files table)
 * - ``name`` is the ``subdir/file.ext`` path relative to the linked folder
 * - ``folder_path`` is the directory prefix of ``name`` with the filename removed, for the frontend to group by folder
 */
export interface ProjectFileItem {
  id: string;
  artifact_id: string;
  name: string;            // ``subfolder/file.ext`` or ``file.ext``
  title: string;
  mime_type: string;
  size_bytes: number;
  download_url: string;
  type: string;
  folder_path?: string;    // ``subfolder`` or ``''``
  created_at: string | null;
}

export type ChatShareScope = 'private' | EditionChatShareScope;

export interface ProjectChatSummary extends EditionProjectChatFields {
  chat_id: string;
  title: string;
  /** team-share scenario: from chat_session_user_states, independent per member. */
  pinned: boolean;
  favorite: boolean;
  message_count: number;
  last_message_at: string | null;
  updated_at: string | null;
  created_at: string | null;
}

/**
 * 引用来源类型。宿主自有的几种在这里列出（用于补全与图标表），插件贡献的
 * 来源类型由 plugin.json 的 tool_meta.citation 定义，是开放字符串，
 * 所以这里用 `(string & {})` 收口而不是把插件的类型写死进联合。
 */
export type CitationSourceType =
  | 'internet'
  | 'knowledge_base'
  | 'database'
  | 'unknown'
  | (string & {});

export interface CitationItem {
  id: string;            // 证据锚点 "e7"；旧格式 "internet_search-1"（历史消息）
  tool_name: string;
  tool_id?: string;
  title: string;
  url: string;
  snippet: string;
  source_type: CitationSourceType;
  /** 条目在该次工具结果列表中的 0-based 下标；整体型为 -1（锚点引用专有） */
  item_index?: number;
}

export type UpdateCategory = '模型迭代' | '信息处理' | '应用上新' | '体验优化';

export interface UpdateEntry {
  date: string;
  year: string;
  title: string;
  category: UpdateCategory;
  desc: string;
}

export type OntologyAssetKind = 'tool' | 'skill' | 'subagent';

export interface OntologyTagPack {
  pack_id: string;
  pack_name: string;
  domain: string;
  version: string;
}

export interface OntologyTagWorkflow {
  workflow_ref: string;
  workflow_name: string;
  review_level: string;
  risk: string;
}

/** A controlled tag declared by an active Domain Pack as an asset workflow trigger. */
export interface OntologyTagOption {
  value: string;
  concept_id: string;
  concept_name: string;
  definition: string;
  risk: string;
  packs: OntologyTagPack[];
  workflows: OntologyTagWorkflow[];
}
