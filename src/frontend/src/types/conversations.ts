import type { ChatRole, EvolutionSummary, MessageSegment, OntologyGovernanceSummary, ThinkingBlock, ToolCall } from './messages';
import type { ChatShareScope, CitationItem } from './workspace';

/** Live plan/progress state rendered by the plan bar above the chat input.
 *  Two sources: the main agent's update_plan tool (source='agent', per-turn
 *  checklist) and the manual plan-mode execution pipeline (source='plan_mode'). */
export interface PlanProgressState {
  source: 'agent' | 'plan_mode';
  title: string;
  steps: Array<{
    title: string;
    status: 'pending' | 'in_progress' | 'completed' | 'failed';
  }>;
  /** Set when the producing stream has ended (bar shows a settled state until the next send) */
  done?: boolean;
  updatedAt: number;
}

/** 一段被引用的历史会话。名片内容由后端按权限现查后随消息落库，前端只负责显示。 */
export interface ReferencedChatCard {
  chat_id: string;
  title: string;
  message_count?: number;
  last_active_display?: string;
  overview?: string;
}

/** 可引用会话列表项（挑选阶段用，不含正文）。 */
export interface ReferencableChat {
  chat_id: string;
  title: string;
  project_id?: string | null;
  message_count: number;
  last_active_at?: string | null;
  last_active_display: string;
}

export interface ChatMessage {
  role: ChatRole;
  content: string;
  isMarkdown?: boolean;
  /** 这条消息的稳定身份：React key、以及一切"指的是哪一条"的逻辑（选中分享、
   *  编辑、点赞、定位气泡）都用它。历史消息取后端主键 message_id，本地还没落库
   *  的取 newMessageUid()。
   *
   *  绝不能拿 ts 顶替：同一轮的提问与回答由后端在同一时刻创建，ts 完全相同，
   *  当 key 用会让 React 认错节点，切换会话时把上一个会话的气泡留在页面顶部。 */
  uid: string;
  /** 消息发生的时刻，只作时间用（展示、计时、滚动锚点），不承担身份。 */
  ts: number;
  quotedFollowUp?: {
    text: string;
    ts?: number;
  };
  /** 这条消息发出时引用的历史会话。 */
  referencedChats?: ReferencedChatCard[];
  skillId?: string;
  skillName?: string;
  pluginName?: string;
  connectorName?: string;
  mentionName?: string;
  messageId?: string;   // backend message_id, used for feedback submission
  toolCalls?: ToolCall[];
  thinking?: ThinkingBlock[];
  ontologyGovernance?: OntologyGovernanceSummary;
  evolution?: EvolutionSummary;
  segments?: MessageSegment[];  // ordered segment list (used by new messages)
  citations?: CitationItem[];   // tool-call citation registry
  followUpQuestions?: string[]; // follow-up questions (clickable to send)
  isStreaming?: boolean;
  /** Set on an assistant row the backend is still writing: the last SSE event
   *  it folded in. Resuming follows the run stream from there. */
  inFlight?: { eventOffset?: number };
  /** Terminal error recorded on the row when its run failed. */
  error?: string;
  /** Total generation duration of this agent answer (ms): total wall-clock time from
   *  initiating the answer to the end of streaming output. Only carried by locally
   *  newly-generated assistant messages; backend history messages lack this field. Used
   *  to show "took X.Xs" next to "regenerate" in the action bar. */
  durationMs?: number;
  /** Backend signals an extended LLM silence; UI replaces streaming dots with a "正在准备调用工具…" indicator. */
  toolPending?: boolean;
  /** Wall-clock ms of the last streaming activity (content / tool / meta event).
   *  Anchors the "正在准备调用工具…" elapsed timer to a persisted value so it
   *  survives component remounts on session switch / page refresh instead of
   *  resetting to zero. */
  lastActivityTs?: number;
  attachments?: Array<{
    origin?: 'local' | 'cloud';
    name: string;
    mime_type?: string;
    file_id?: string;       // OSS file ID; downloadable when present
    download_url?: string;  // download path, e.g. /files/{file_id}
  }>;  // files uploaded by the user
  /**
   * Workspace allowlist: file_ids the agent pinned via pin_to_workspace.
   * When set, only these file_ids render as artifact cards (everything else
   * from tool outputs is hidden). When undefined/null, legacy behavior
   * (show every file_id extracted from tool outputs).
   */
  workspaceFiles?: string[] | null;
}

export type ContextUsageSource = 'provider' | 'backend_estimate' | 'compaction_estimate';

/** Latest primary-model context snapshot.
 *
 * provider snapshots have an exact headline total from upstream usage. Their
 * category mix is reconciled from the backend's final request manifest.
 * Estimates are explicit fallbacks used before an upstream measurement exists.
 */
export interface ContextUsageSnapshot {
  source: ContextUsageSource;
  exact: boolean;
  usedTokens: number;
  promptTokens: number;
  completionTokens: number;
  contextWindow: number;
  modelName?: string;
  modelProviderId?: string;
  modelCallIndex: number;
  breakdown: {
    messages: number;
    tools: number;
    thinking: number;
    files: number;
    system: number;
    input: number;
    total: number;
  };
}

/** Latest backend context-compaction checkpoint used by the context gauge.
 * The full transcript stays visible; messages covered by this boundary have
 * been replaced in the model context by a compacted baseline. */
export interface ContextCompactionState {
  checkpointId: string;
  checkpointTs: number;
  coveredThroughMessageId?: string;
  coveredMessageCount: number;
  replacementTokens: number;
  /** Post-compaction backend estimate, present until a newer provider call supersedes it. */
  contextUsage?: ContextUsageSnapshot;
}

export interface ChatItem {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  messages: ChatMessage[];
  favorite?: boolean;
  pinned?: boolean;
  businessTopic?: string;
  /** 用户手动重命名过：自动摘要标题不再覆盖；随会话 metadata.title_manually_set 持久化 */
  titleManuallySet?: boolean;
  /** Sub-agent binding (set when chat is started from a sub-agent) */
  agentId?: string;
  agentName?: string;
  /** Historical classification: this chat was created/used in manual plan mode.
   *  Kept for sidebar/header markers and persisted plan-card reconstruction; it does not
   *  determine how the next composer message is sent. */
  planChat?: boolean;
  /** Explicit composer preference for this chat. Undefined preserves the legacy default
   *  (plan chats start with plan mode enabled); false records that the user switched back
   *  to ordinary conversation while retaining the historical plan cards. */
  planModeActive?: boolean;
  /** 这段对话选中的模式 slug（chat_modes.slug）。undefined = 标准模式。随对话记录
   *  持久化（localStorage），刷新/切对话时由 resolveModeSlug 恢复，不然模式位每次
   *  都掉回标准模式。 */
  modeSlug?: string;
  /** 这段对话选中的思考强度档（chatMode）。undefined = 没显式选过，恢复时沿用当前
   *  会话档位（刷新则回管理端默认）。与 modeSlug 同一套持久化机制。 */
  thinkingEffort?: 'turbo' | 'fast' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';
  /** Historical classification: this chat was created/used via the batch-execution ("批量执行")
   *  entry. Like planChat it survives the user leaving the mode, so batch history stays
   *  recognisable; it does not by itself decide how the next message is sent. */
  batchChat?: boolean;
  /** Explicit composer preference for this chat. Undefined preserves the legacy default
   *  (batch chats start in batch mode); false records that the user closed the mode from
   *  the composer chip / "+" menu and continues as an ordinary conversation. */
  batchModeActive?: boolean;
  /** 历史标记：这段对话用过「工作流模式」（批量作业）。与 planChat/batchChat 同性质——
   *  用户离开模式后仍保留，便于在列表里认出这类会话；它本身不决定下一条消息怎么发。 */
  workflowChat?: boolean;
  /** 这段对话当前的工作流模式开关（用户在 / 命令或「+」菜单里选的）。undefined 走历史默认，
   *  false 表示用户显式关掉了。只有它为 true 才会注册 run_job 并注入批量作业提示词。 */
  workflowModeActive?: boolean;
  /** 服务端记着的任务计划清单（会话 metadata.plan_progress）。计划栏本身是内存态、
   *  一刷新就没，而工作流的一份计划要跨好几轮（提交作业 → 后台跑 → 交付轮收尾）才走完；
   *  这份快照就是刷新/切回来之后把计划栏还原成真实状态的依据。 */
  planProgress?: PlanProgressState;
  /** Whether this chat was created via the site-building ("站点建站") entry (Lab → Sites) */
  siteChat?: boolean;
  /** Automation task ID — set on virtual sidebar entries for automation tasks */
  automationTaskId?: string;
  /** Whether this is an automation-generated chat (virtual sidebar entry) */
  automationRun?: boolean;
  /** 混合架构（桌面双模式）：该对话的运行位置。'local' = 在本机执行面运行、
   *  会话保存在本机；'cloud' = 显式选择云端；旧记录未设置仍为云端。新草稿默认本机。
   *  绑定项目的对话跟随项目归属。 */
  runTarget?: 'local' | 'cloud';
  /** Which project this chat is mounted under (Claude-style workspaces).
   *  When present, sending a message automatically attaches project_id so the backend
   *  injects the project instructions / folder scope into ctx. */
  projectId?: string;
  /** Display name of the chat's project (cached at binding time, used for the "project name / title" breadcrumb in the chat header). */
  projectName?: string;
  /** Session-level sharing switch (only effective in team projects with the project-level sharing switch ON). */
  shareScope?: ChatShareScope;
  /** Session creator's user_id (sharing scenarios need to show "created by: xxx"). */
  ownerUserId?: string;
  /** Session creator's display name. */
  ownerName?: string;
  /** The current user's access level for this chat (sharing scenarios). */
  accessLevel?: 'admin' | 'edit' | 'read';
  /** Whether the current user is the creator of this chat. */
  isOwner?: boolean;
}

export interface ChatStore {
  chats: Record<string, ChatItem>;
  order: string[];
}
