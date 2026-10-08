export type ChatRole = 'user' | 'assistant';

/**
 * One streaming sub-step inside a sub-agent (call_subagent) — rendered under the
 * parent call_subagent tool card. Delivered by the backend via the `subagent_event`
 * SSE event, correlated by parent_tool_id.
 */
export interface SubagentStep {
  kind: 'tool' | 'thinking' | 'content';
  // When kind === 'tool': one tool call made by the sub-agent
  toolId?: string;
  name?: string;
  displayName?: string;
  input?: any;
  /** Incremental JSON argument text while the model is constructing the call. */
  inputText?: string;
  output?: any;
  status?: 'running' | 'success' | 'error' | 'interrupted';
  // When kind === 'thinking' | 'content': the accumulated text
  text?: string;
}

export interface ToolCall {
  id?: string;
  name: string;
  displayName?: string;
  input?: any;
  /** Incremental JSON argument text retained for the live tool-call view. */
  inputText?: string;
  output?: any;
  status?: 'pending' | 'running' | 'success' | 'error' | 'interrupted';
  /** 调用开始的墙钟时刻（服务端下发并落库），卡上的计时以它为起点。 */
  timestamp?: number;
  /** 这次调用实际花了多久（服务端算好并落库）。收尾后显示的就是它——历史重放
   *  时两个端点时刻都已不在，只有存下来的耗时是可信的。 */
  durationMs?: number;
  // call_subagent only: the sub-agent's internal streaming sub-steps + the sub-agent's name
  subSteps?: SubagentStep[];
  subagentName?: string;
  /** 运行中工具的实时进度短句（run_job 的批量作业进度等）。只活在流里，不落库——
   *  工具跑完后由 tool_result 说明结局，历史里不该留一行过期数字。 */
  progressNote?: string;
  scope?: 'ontology_revision' | string;
  /** 历史列表只给了梗概，完整结果要展开时按需回取（后端 result_truncated）。 */
  outputTruncated?: boolean;
  /** 已经按需取回过完整结果，别重复请求。 */
  outputLoaded?: boolean;
}

/** §13 MySpace write confirmation decision (literal counterparts of the backend's _myspace_confirm.DECISION_*). */
export type FileConfirmDecision = 'allow' | 'allow_session' | 'deny';

/** §13 MySpace write / automation-task change confirmation: pending-confirmation info (delivered by the backend via the file_confirm SSE event). */
export interface FileConfirmInfo {
  confirmId: string;
  op: string;
  logicalPath: string;
  message?: string;
  /** Confirmation category: 'myspace' (MySpace write, default) | 'automation' (automation-task change). */
  kind?: string;
}

/** Site-building design pick (choose 1 of 3): a single candidate option (registered by the backend choose_design tool). */
export interface DesignPickOption {
  id: string;
  title: string;
  brief?: string;
  /** artifact file_id of the preview screenshot; the frontend displays it inline via /files/{id}?inline=true. */
  imageFileId: string;
}

/** Site-building design pick (choose 1 of 3): pending-selection info (delivered by the backend via the design_pick SSE event). */
export interface DesignPickInfo {
  confirmId: string;
  question: string;
  options: DesignPickOption[];
}

/** One stable choice in an assistant-initiated user question. */
export interface UserQuestionOption {
  id: string;
  label: string;
  description?: string;
  recommended: boolean;
}

/** One question rendered by the resident composer. */
export interface UserQuestionItem {
  id: string;
  header?: string;
  question: string;
  description?: string;
  options: UserQuestionOption[];
  multiSelect: boolean;
}

/** One pending tool request; chats may queue several parallel requests. */
export interface UserQuestionRequest {
  requestId: string;
  questions: UserQuestionItem[];
  createdAt?: number;
  expiresAt?: number;
}

export interface UserQuestionAnswer {
  id: string;
  selected: string[];
  custom?: string;
  skipped?: boolean;
}

export interface ThinkingBlock {
  content: string;
  timestamp?: number;
}

/**
 * 一条历史消息的展示顺序，落库在 `metadata.segments`。
 *
 * 顺序在实时流式的那一刻就已确定，后端原样记下来；刷新后照着渲染，不再从字符偏移
 * 之类的辅助字段反推。正文片段内联，思考与工具卡片按下标引用 `thinking` /
 * `tool_calls` 两列，长推理不会被存两份。
 */
export type StoredSegment =
  | { type: 'text'; text: string }
  | { type: 'thinking'; index: number }
  | { type: 'tool'; index: number };

export interface OntologyActivationSummary {
  pack_id?: string;
  workflow_id?: string;
  workflow_name?: string;
  source?: 'text' | 'tool' | 'skill' | 'subagent' | string;
  asset_kind?: string;
  asset_id?: string;
  review_level?: string;
}

export interface OntologyGateSummary {
  decision?: 'pass' | 'deny' | string;
  tool_name?: string;
  matched_rule_ids?: string[];
  violations?: string[];
  denial_count?: number;
  circuit_breaker?: boolean;
}

export interface OntologyReviewSummary {
  status?: 'pending' | 'running' | 'completed' | 'failed' | string;
  level?: 'none' | 'checkpoint' | 'committee' | string;
  owner?: string | null;
  count?: number;
  verdict?: 'pass' | 'revise' | 'escalate' | string;
  revised?: boolean;
  latency_ms?: number;
  committee_size?: number;
  candidate_answer?: string;
  accepted?: boolean;
  violations?: Array<Record<string, unknown>>;
  affected_claims?: OntologyManualReviewItem[];
  evidence?: string[];
  feedback?: string[];
  error?: string;
  manual_review?: OntologyManualReview;
  new_tools?: string[];
  new_citation_count?: number;
}

export interface OntologyManualReviewItem {
  quote: string;
  rule_id: string;
  risk: string;
  manual_check: string;
}

export interface OntologyManualReview {
  required: boolean;
  title: string;
  summary: string;
  items: OntologyManualReviewItem[];
  actions: string[];
}

export interface OntologyRevisionSummary {
  status: 'pending' | 'streaming' | 'completed' | string;
  source?: string;
  content: string;
  thinking: ThinkingBlock[];
  toolCalls: ToolCall[];
  toolCallCount?: number;
  toolPending?: boolean;
}

/** User-visible ontology governance evidence, intentionally separate from model thinking. */
/**
 * One memory a turn wrote.
 *
 * `handle` is what makes the row actionable — a profile field key for L1, a
 * mem0 id for L2, or a Neo4j relation id for L3 — and decides which API the
 * actions call. An entry without one is never emitted by the backend.
 */
export interface EvolutionMemoryEntry {
  /** `L1` = user profile, `L2` = procedure, `L3` = entity relation. */
  layer: 'L1' | 'L2' | 'L3';
  handle: string;
  text: string;
  kind?: string;
  /** Why the rule holds — what tells a reader where it stops applying. */
  why?: string;
  /** The task family it was stated for. */
  applies_to?: string;
  action?: string;
}

export interface EvolutionSummary {
  episode_id?: string;
  message_id?: string;
  /** `empty` means nothing was written — render no card at all. */
  state: 'pending' | 'settled' | 'failed' | 'empty';
  /** `written` or `failed`; memory is the only mechanism a turn can report. */
  status?: 'written' | 'failed' | '';
  gain?: number;
  entries?: EvolutionMemoryEntry[];
  error?: string;
  settled_at?: string;
}

export interface OntologyGovernanceSummary {
  governance_run_id?: string;
  activations: OntologyActivationSummary[];
  gates: OntologyGateSummary[];
  review: OntologyReviewSummary;
  revision?: OntologyRevisionSummary;
}

/** Records the order of a message's elements (text/tool calls/thinking) for inline interleaved rendering */
export interface MessageSegment {
  type: 'text' | 'tool' | 'thinking' | 'plan';
  content?: string;    // used for 'text' and 'thinking' types
  toolIndex?: number;  // used for 'tool' type; refers to toolCalls[toolIndex]
  planData?: {         // used for 'plan' type
    mode: 'preview' | 'executing' | 'complete';
    planId?: string;   // associated plan_id — used to restore the "pending-confirmation plan" from history messages after refresh
    /** Approval decision made on the preview card (hides the confirm/discard buttons afterwards) */
    decided?: 'confirmed' | 'cancelled';
    title: string;
    description?: string;
    steps: Array<{
      step_order: number;
      title: string;
      description?: string;
      expected_tools?: string[];
      expected_skills?: string[];
      expected_agents?: string[];
      acceptance_criteria?: string;
      status?: 'pending' | 'running' | 'success' | 'failed' | 'skipped';
      summary?: string;
      text?: string;
    }>;
    completedSteps?: number;
    totalSteps?: number;
    resultText?: string;
    agentNameMap?: Record<string, string>;
    /** 执行被用户中断：卡片显示「已中断」而不是继续挂在「执行中」上（问题 31）。 */
    cancelled?: boolean;
  };
}
