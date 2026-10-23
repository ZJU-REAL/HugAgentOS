import { t } from '../i18n';
import { resolvePluginToolLabel, resolveToolDisplayName } from './toolMeta';
import type { ToolIconKind } from './toolIconKind';

/**
 * 折叠态执行卡那一行的文案真源。
 *
 * 过程展示默认收起成一行，用户不点开就只看得到这句话——所以它必须自己说清楚
 * 在做什么（"正在执行命令" / "运行了命令"），而不是笼统的"执行中 / 已完成"。
 */
interface ToolAction {
  running: string;
  done: string;
  /** Line icon for this kind of action (components/tool/toolIcons.tsx). */
  icon: ToolIconKind;
}

const action = (running: string, done: string, icon: ToolIconKind): ToolAction => ({ running, done, icon });

const SHELL = action(t('正在执行命令'), t('运行了命令'), 'terminal');
const FILE_READ = action(t('正在读取文件'), t('读取了文件'), 'fileRead');
const KB_QUERY = action(t('正在检索知识库'), t('检索了知识库'), 'knowledge');
const KNOWLEDGE_MAP = action(t('正在查阅知识地图'), t('查阅了知识地图'), 'graph');
const DB_QUERY = action(t('正在查询数据库'), t('查询了数据库'), 'database');
const SKILL = action(t('正在激活技能'), t('激活了技能'), 'skill');
const CAPABILITY_LIST = action(t('正在获取能力列表'), t('获取了能力列表'), 'catalog');
const WORD_DOC = action(t('正在生成 Word 文档'), t('生成了 Word 文档'), 'word');
const CHAT_HISTORY = action(t('正在查阅历史对话'), t('查阅了历史对话'), 'chats');

/** The one table of host-owned tools: what the row says and which icon it shows. */
const TOOL_ACTIONS: Record<string, ToolAction> = {
  Bash: SHELL,
  bash: SHELL,
  run_command: SHELL,
  execute_code: SHELL,
  Read: FILE_READ,
  view_text_file: FILE_READ,
  read_artifact: FILE_READ,
  Write: action(t('正在写入文件'), t('写入了文件'), 'fileWrite'),
  Edit: action(t('正在编辑文件'), t('编辑了文件'), 'edit'),
  Glob: action(t('正在查找文件'), t('查找了文件'), 'findFiles'),
  Grep: action(t('正在搜索内容'), t('搜索了内容'), 'grep'),
  read_image: action(t('正在识别图片'), t('识别了图片'), 'image'),
  internet_search: action(t('正在联网搜索'), t('搜索了网页'), 'webSearch'),
  web_fetch: action(t('正在获取网页'), t('获取了网页'), 'globe'),
  retrieve_dataset_content: KB_QUERY,
  retrieve_local_kb: KB_QUERY,
  list_datasets: KB_QUERY,
  search_knowledge_base: KB_QUERY,
  wiki_overview: KNOWLEDGE_MAP,
  wiki_locate: KNOWLEDGE_MAP,
  wiki_read_page: KNOWLEDGE_MAP,
  wiki_expand: KNOWLEDGE_MAP,
  wiki_fetch_source: KNOWLEDGE_MAP,
  query_database: DB_QUERY,
  db_query: DB_QUERY,
  execute_sql: DB_QUERY,
  search_objects: DB_QUERY,
  load_skill: SKILL,
  use_skill: SKILL,
  load_plugin: action(t('正在加载插件'), t('加载了插件'), 'plugin'),
  get_skills: CAPABILITY_LIST,
  get_mcp_tools: CAPABILITY_LIST,
  get_agents: CAPABILITY_LIST,
  call_subagent: action(t('正在调用智能体'), t('调用了智能体'), 'agent'),
  ask_user_question: action(t('正在向你提问'), t('向你提了问题'), 'ask'),
  run_job: action(t('正在运行批量作业'), t('运行了批量作业'), 'job'),
  sandbox_put_artifact: action(t('正在读写空间文件'), t('读写了空间文件'), 'folderUp'),
  sandbox_get_artifact: action(t('正在读写空间文件'), t('读写了空间文件'), 'folderDown'),
  pin_to_workspace: action(t('正在固定到工作区'), t('固定到了工作区'), 'pin'),
  word_create_from_markdown: WORD_DOC,
  export_report_to_docx: WORD_DOC,
  export_table_to_excel: action(t('正在导出 Excel 表格'), t('导出了 Excel 表格'), 'table'),
  publish_site: action(t('正在发布站点'), t('发布了站点'), 'publish'),
  list_related_chats: CHAT_HISTORY,
  read_chat: CHAT_HISTORY,
  read_project_instructions: action(t('正在读取项目说明'), t('读取了项目说明'), 'notes'),
  save_project_instructions: action(t('正在更新项目说明'), t('更新了项目说明'), 'notes'),
};

/** Icon kind for a tool; unregistered tools (connectors, plugin tools) fall back to the wrench. */
export function toolIconKind(toolName: string): ToolIconKind {
  return TOOL_ACTIONS[toolName]?.icon ?? 'wrench';
}

/** 折叠行只认名字，不需要整个 ToolCall。 */
export interface LabeledTool {
  name: string;
  displayName?: string;
}

/**
 * 没登记在上表里的工具——MCP 连接器、插件贡献、EE 专属工具——一律报它自己的
 * 名字，而不是笼统的"正在调用工具"：用户要看的就是实际调了什么。
 */
function fallbackAction(tool: LabeledTool): ToolAction {
  const label = resolvePluginToolLabel(tool.name) || resolveToolDisplayName(tool);
  if (!label) return action(t('正在调用工具'), t('已调用工具'), 'wrench');
  return action(t('正在调用 {label}', { label }), t('已调用 {label}', { label }), 'wrench');
}

function actionOf(tool: LabeledTool): ToolAction {
  return TOOL_ACTIONS[tool.name] || fallbackAction(tool);
}

/** 运行中的那一行：跟着当前正在跑的工具走。 */
export function getRunningActionLabel(tool: LabeledTool): string {
  return actionOf(tool).running;
}

/**
 * 收尾后的那一行：这一批只用了一种工具就报它的名字，混用多种则报通用文案
 * （工具数已经在标题右侧单独显示，不必在这里重复）。
 */
export function getDoneActionLabel(tools: LabeledTool[]): string {
  if (tools.length === 0) return t('思考完成');
  const first = actionOf(tools[0]).done;
  return tools.every((tool) => actionOf(tool).done === first) ? first : t('已调用工具');
}
