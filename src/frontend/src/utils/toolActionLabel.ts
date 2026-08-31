import { t } from '../i18n';
import { resolvePluginToolLabel, resolveToolDisplayName } from './toolMeta';

/**
 * 折叠态执行卡那一行的文案真源。
 *
 * 过程展示默认收起成一行，用户不点开就只看得到这句话——所以它必须自己说清楚
 * 在做什么（"正在执行命令" / "运行了命令"），而不是笼统的"执行中 / 已完成"。
 */
interface ToolAction {
  running: string;
  done: string;
}

const FILE_READ: ToolAction = { running: t('正在读取文件'), done: t('读取了文件') };
const FILE_WRITE: ToolAction = { running: t('正在写入文件'), done: t('写入了文件') };
const KB_QUERY: ToolAction = { running: t('正在检索知识库'), done: t('检索了知识库') };
const CAPABILITY_LIST: ToolAction = { running: t('正在获取能力列表'), done: t('获取了能力列表') };
const SPACE_FILE: ToolAction = { running: t('正在读写空间文件'), done: t('读写了空间文件') };
const CHAT_HISTORY: ToolAction = { running: t('正在读取会话记录'), done: t('读取了会话记录') };
const WORD_DOC: ToolAction = { running: t('正在生成 Word 文档'), done: t('生成了 Word 文档') };

const TOOL_ACTIONS: Record<string, ToolAction> = {
  bash: { running: t('正在执行命令'), done: t('运行了命令') },
  Write: FILE_WRITE,
  Edit: { running: t('正在编辑文件'), done: t('编辑了文件') },
  view_text_file: FILE_READ,
  internet_search: { running: t('正在联网搜索'), done: t('搜索了网页') },
  web_fetch: { running: t('正在获取网页'), done: t('获取了网页') },
  retrieve_dataset_content: KB_QUERY,
  retrieve_local_kb: KB_QUERY,
  list_datasets: KB_QUERY,
  query_database: { running: t('正在查询数据库'), done: t('查询了数据库') },
  load_skill: { running: t('正在激活技能'), done: t('激活了技能') },
  load_plugin: { running: t('正在加载插件'), done: t('加载了插件') },
  get_skills: CAPABILITY_LIST,
  get_mcp_tools: CAPABILITY_LIST,
  get_agents: CAPABILITY_LIST,
  call_subagent: { running: t('正在调用智能体'), done: t('调用了智能体') },
  list_myspace_files: SPACE_FILE,
  stage_myspace_file: SPACE_FILE,
  sandbox_put_artifact: SPACE_FILE,
  sandbox_get_artifact: SPACE_FILE,
  list_favorite_chats: CHAT_HISTORY,
  get_chat_messages: CHAT_HISTORY,
  generate_chart_tool: { running: t('正在生成图表'), done: t('生成了图表') },
  word_create_from_markdown: WORD_DOC,
  export_report_to_docx: WORD_DOC,
  export_table_to_excel: { running: t('正在导出 Excel 表格'), done: t('导出了 Excel 表格') },
};

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
  if (!label) return { running: t('正在调用工具'), done: t('已调用工具') };
  return {
    running: t('正在调用 {label}', { label }),
    done: t('已调用 {label}', { label }),
  };
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
