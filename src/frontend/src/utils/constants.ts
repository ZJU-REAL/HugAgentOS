import type { PanelKey } from '../types';
import { t } from '../i18n';
import { EDITION_TOOL_NAME_OVERRIDES } from '../toolEdition';

export type CatalogKind = Exclude<PanelKey, 'chat' | 'docs' | 'app_center' | 'share_records' | 'settings'>;

/** Tool names whose output should open in the right-side panel (not inline) */
export const PANEL_TOOL_NAMES = new Set([
  'query_database',
  'retrieve_dataset_content',
  'retrieve_local_kb',
  'list_datasets',
  'internet_search',
  'get_industry_news',
  'get_latest_ai_news',
  'get_chain_information',
  'search_company',
  'get_company_base_info',
  'get_company_business_analysis',
  'get_company_tech_insight',
  'get_company_funding',
  'get_company_risk_warning',
]);

/** Icons for each tool (under /icons/) */
export const TOOL_ICONS: Record<string, string> = {
  query_database: '/icons/database.png',
  retrieve_dataset_content: '/icons/knowledge.png',
  retrieve_local_kb: '/icons/knowledge.png',
  list_datasets: '/icons/knowledge.png',
  internet_search: '/icons/internet.png',
  web_fetch: '/icons/internet-mcp.svg',
  get_industry_news: '/icons/news.png',
  get_latest_ai_news: '/icons/ai-news.png',
  get_chain_information: '/icons/industry-chain.png',
  search_company: '/icons/industry-chain.png',
  get_company_base_info: '/icons/industry-chain.png',
  get_company_business_analysis: '/icons/industry-chain.png',
  get_company_tech_insight: '/icons/industry-chain.png',
  get_company_funding: '/icons/industry-chain.png',
  get_company_risk_warning: '/icons/industry-chain.png',
};

/**
 * AgentScope 2.0 used to name MCP tools as `mcp__<server>__<tool>` (the backend now restores the bare
 * name at the source, but older session histories persisted the prefixed tool_name / tool_display_name).
 * Display and mapping (TOOL_NAME_OVERRIDES / toolDisplayNames / TOOL_ICONS / PANEL_TOOL_NAMES)
 * always match on the server-side bare name, so messages are normalized through this function on ingest.
 */
export function stripMcpToolPrefix(name: string): string {
  const m = /^mcp__(.+?)__(.+)$/.exec(name);
  return m ? m[2] : name;
}

/** Frontend-local tool name overrides (higher priority than backend displayName) */
export const TOOL_NAME_OVERRIDES: Record<string, string> = {
  view_text_file: t('读取文件'),
  load_skill: t('加载技能'),
  // MySpace tools
  list_myspace_files: t('浏览我的空间'),
  stage_myspace_file: t('导入文件到工作区'),
  list_favorite_chats: t('浏览收藏会话'),
  get_chat_messages: t('读取会话记录'),
  ...EDITION_TOOL_NAME_OVERRIDES,
};

export const TOPIC_TAG_COLORS: Record<string, string> = {
  '综合咨询': 'default',
  '政策解读': 'blue',
  '事项办理': 'cyan',
  '材料比对': 'purple',
  '知识检索': 'geekblue',
  '数据分析': 'green',
};

export function isCatalogKind(kind: PanelKey): kind is CatalogKind {
  return kind === 'skills' || kind === 'agents' || kind === 'mcp' || kind === 'kb';
}

/** Max rounds to refresh the summary title */
export const SUMMARY_MAX_ROUNDS = 3;

/** The skill marketplace's fixed 8 categories (the only selectable set). See the backend constant of the same name in core/services/marketplace_service.py. */
export const MARKETPLACE_CATEGORIES = [
  '写作助手', '文档处理', '数据分析', '政策产业',
  '营销创意', '法务合规', '办公效率', '研发效率',
] as const;

/** The sub-agent marketplace's fixed 9 categories. See the backend constant of the same name in core/services/agent_market_categories.py. */
export const AGENT_MARKETPLACE_CATEGORIES = [
  '通用助手', '职场办公', '商业分析', '数据分析', '研发编程',
  '翻译写作', '创意设计', '政策法务', '教育科研',
] as const;

/** folder_id sentinel for GET /v1/artifacts: root directory only (user_folder_id IS NULL). See the backend constant of the same name in core/db/repository.py. */
export const ROOT_FOLDER_SENTINEL = '__root__';

/** API base URL (e.g. '/api') */
export const getApiBase = (): string =>
  (import.meta.env.VITE_API_BASE_URL as string || '/api').replace(/\/+$/, '');

/** Build a direct download URL for an artifact file. */
export const buildFileUrl = (fileId: string): string =>
  `${getApiBase()}/files/${fileId}`;
