import { message } from 'antd';
import { t } from '../i18n';
import {
  getChatContextState,
  listChatJobs,
  toDesignPickInfo,
  toFileConfirmInfo,
  toUserQuestionRequest,
} from '../api';
import { normalizeArtifactOutput } from '../utils/fileParser';
import { stripMcpToolPrefix } from '../utils/constants';
import { refreshTargetForTool } from '../utils/toolRefresh';
import {
  isCompactionCheckpointForRun,
  parseContextCompactionState,
  parseContextUsageSnapshot,
} from '../utils/contextUsage';
import { parseQueuedRunHandoff, type QueuedRunHandoff } from '../utils/streamHandoff';
import { readText, resolveText } from '../plugin-ui';
import { usePluginUiStore, type CanvasTarget } from '../stores/pluginUiStore';
import { isMobileViewport } from './useIsMobileViewport';
import {
  appendStreamTextSegment,
  appendSubagentStepDelta,
  appendThinkingContentBeforeTrailingText,
  deferThinkingTextFragmentBeforeTool,
  liftTrailingSegmentsAboveFinalText,
  restoreDeferredThinkingTextFragment,
  type DeferredThinkingTextFragment,
} from '../utils/streamSegments';
import { useChatStore, useCatalogStore, useUIStore, useBatchStore, useCanvasStore, useAgentStore, usePluginStore } from '../stores';
import type { ChatItem, ChatMessage, CitationItem, EvolutionSummary, MessageSegment, OntologyGovernanceSummary, SubagentStep, ToolCall } from '../types';
import { writeLocal } from '../storage';

/**
 * Unified chat SSE stream processor (single source of truth).
 *
 * Send, regenerate/edit-resend, reconnect replay (follow), batch cancel-and-resume, and
 * autonomous loop (loop start/resume/follow) **all** go through this one processor: the same
 * event vocabulary (content/thinking/tool_call_start/tool_call_delta/tool_call/tool_result/meta/…),
 * the same <think> stripping
 * state machine, and the same bubble rendering pipeline.
 *
 * Path-specific events (e.g. the autonomous loop's loop_started/loop_plan/…) are intercepted via
 * the `onEvent` hook before built-in handling — the hook only owns its own extra UI (plan bar
 * etc.); bubble rendering is still done uniformly by this processor. Copying this file's
 * reduction logic for new scenarios is forbidden.
 */

/* ───────────────────────────────────────────
   SSE 传输层活性表 —— 「这条流上一次收到字节是什么时候」。

   为什么要单独记一份：后端每 15 秒会往流里写一行 `: heartbeat` 注释，它不产生任何
   可渲染事件，所以只看气泡内容根本分不清「模型在长工具里干活（正常）」和「连接早就
   断了但 fetch 没报错（半开挂死）」。后者的表现是气泡永远转圈、刷新一下才发现后台
   其实早跑完了——这份时间戳就是用来把这两种情况分开的唯一依据。

   记的是**任何字节**（含心跳），不是事件：一个跑 50 分钟的批量作业期间可以完全没有
   可渲染事件，但心跳一刻不停；心跳都停了才是真断了。
   ─────────────────────────────────────────── */
const _streamActivity = new Map<string, number>();

/** 该会话的流上一次收到字节的时刻（毫秒）；没有在跟随的流则为 0。 */
export function getStreamActivityTs(chatId: string): number {
  return _streamActivity.get(chatId) || 0;
}

/** 本标签页已经（正在或曾经）流过的 run。
 *
 *  给"后端自己发起的那一轮"用：轮询看到一个活的 run 时，得能分清它是刚被唤醒起来的
 *  新轮次，还是自己这一轮刚跑完、后端状态还没落终态的残影——认错了就会把同一轮从头
 *  重放一遍，气泡直接翻倍。run_started 是每条流的第一帧（重放也带），拿它当身份证。 */
const _seenRuns = new Set<string>();

export function hasStreamedRun(runId: string): boolean {
  return _seenRuns.has(runId);
}

const COMPACTION_REFRESH_DELAYS_MS = [500, 1_000, 2_000, 4_000, 8_000, 12_000, 16_000, 20_000];

async function refreshContextAfterCompaction(
  chatId: string,
  previousCheckpointId: string,
  runStartedAt: number,
  expectedCoveredMessageId?: string,
): Promise<void> {
  for (const delayMs of COMPACTION_REFRESH_DELAYS_MS) {
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    try {
      const state = await getChatContextState(chatId);
      const compaction = parseContextCompactionState(state.context_compaction);
      const isNewCheckpoint = isCompactionCheckpointForRun(
        compaction,
        previousCheckpointId,
        runStartedAt,
        expectedCoveredMessageId,
      );
      if (!isNewCheckpoint || !compaction) continue;
      const store = useChatStore.getState();
      store.setContextCompaction(chatId, compaction);
      // Older checkpoints may not carry a replacement snapshot. In that case
      // retain the latest provider measurement rather than fabricating a drop.
      if (!compaction.contextUsage) {
        const usage = parseContextUsageSnapshot(state.context_usage);
        if (usage) store.setContextUsage(chatId, usage);
      }
      return;
    } catch {
      // Background compaction is best-effort. Keep polling within the bounded
      // summarizer window and leave the last provider measurement intact.
    }
  }
}

/** 用户按过「停止」的 run。
 *
 *  停止会调 /v1/chat-runs/{id}/cancel，但那是 fire-and-forget：请求失败、后端
 *  协作式取消还没落终态、或者这一轮的 run_id 压根没拿到（多窗口时跟随权在另一个
 *  窗口）时，DB 里的 run 仍是 running。切走再切回来，resumeRunIfAny / 断连看门狗
 *  就会把它当成"还活着的后台任务"重新挂上并从 offset 0 全量重放 —— 用户看到的
 *  就是"已经中断的计划任务自己又开始执行了"。
 *
 *  用户的停止意图是终局的：登记下来，任何重挂路径都不许再跟随这个 run。写
 *  localStorage 是为了多窗口 / 刷新后同样生效（用户就是在两个窗口之间来回切时
 *  撞上这个问题的）。 */
const CANCELLED_RUNS_KEY = 'hugagent_ui_cancelled_runs_v1';
const CANCELLED_RUNS_MAX = 100;
const _cancelledRuns = new Set<string>();

function loadCancelledRuns(): Set<string> {
  if (typeof window === 'undefined') return _cancelledRuns;
  try {
    const raw = window.localStorage.getItem(CANCELLED_RUNS_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (Array.isArray(parsed)) for (const id of parsed) if (typeof id === 'string') _cancelledRuns.add(id);
  } catch { /* ignore */ }
  return _cancelledRuns;
}

export function markRunCancelledByUser(runId: string): void {
  if (!runId) return;
  loadCancelledRuns();
  _cancelledRuns.add(runId);
  if (typeof window === 'undefined') return;
  try {
    const next = [..._cancelledRuns].slice(-CANCELLED_RUNS_MAX);
    writeLocal(CANCELLED_RUNS_KEY, JSON.stringify(next));
  } catch { /* ignore */ }
}

export function isRunCancelledByUser(runId: string): boolean {
  if (!runId) return false;
  return loadCancelledRuns().has(runId);
}

/** 管理类插件写操作后，重拉持有那份列表的 store。
 *
 *  刷新不是锦上添花：MCP 跑在自己的容器里，它清掉的能力缓存是**它那个进程**的，不是 backend
 *  的——前端这次重拉才是让变更真正可见的那一步，否则就是"说创建好了但界面没动静"。
 *
 *  刷哪个 store 与刷不刷同样重要，映射表见 utils/toolRefresh.ts（那里也有测试钉住）。
 *  pluginStore 有 `loaded` 缓存，所以要 fetchInstalled(true) 强制。 */
function maybeRefreshCatalogAfterTool(toolName: string, status: string): void {
  if (status !== 'success') return;
  const target = refreshTargetForTool(stripMcpToolPrefix(toolName || ''));
  if (!target) return;
  if (target === 'catalog') void useCatalogStore.getState().fetchCatalog();
  else if (target === 'agents') void useAgentStore.getState().fetchAgents();
  else {
    void usePluginStore.getState().fetchInstalled(true);
    // Installing/uninstalling a plugin also changes which tools have a
    // contributed card, so the UI registry has to be re-pulled alongside it.
    void usePluginUiStore.getState().fetchContributions(true);
  }
}

/**
 * Canvas 在移动断点是**整屏覆盖**（mobile.css 把 .jx-canvasPanelSlot 铺成 inset:0），
 * 自动弹出等于把正在读的对话整页顶掉——插件的边跑边出图类画布尤其难受：
 * 用户还在看推理过程，画布一到就全屏盖住，得先找关闭键才能回到对话。
 * 所以移动端一律不自动弹，只在消息里留卡片入口（插件视图卡 / 附件卡 / 本体评审入口），
 * 由用户主动点开。桌面端行为不变（右侧分栏，不遮挡正文）。
 */
function canAutoOpenCanvas(): boolean {
  return !isMobileViewport();
}

function canOpenPluginCanvasForChat(chatId: string): boolean {
  return useChatStore.getState().currentChatId === chatId
    && useCatalogStore.getState().panel === 'chat'
    && canAutoOpenCanvas();
}

/** What an installed plugin asked to put in the canvas while this tool runs. */
function findAutoCanvas(toolName: string | undefined): CanvasTarget | null {
  if (!toolName) return null;
  return usePluginUiStore.getState().findCanvasTargetForTool(toolName);
}

/**
 * Tab label for an auto-opened canvas.
 *
 * A plugin may point `title_from_input` at one of the tool's arguments so the
 * tab reads "锂电池" rather than a generic view name; otherwise the contributed
 * title is used.
 */
function canvasTabTitle(target: CanvasTarget, toolInput: unknown): string {
  const fromInput = target.titleFromInput ? readText(toolInput, target.titleFromInput) : '';
  return fromInput || resolveText(target.title);
}

/** Unified handling of the site-design pick-one-of-three SSE event (shared by the live stream
 *  and the replay/follow path): expired → dismiss the card; otherwise parse the options and
 *  drop them into the single pendingDesignPick slot. */
function applyDesignPickEvent(chatId: string, obj: Record<string, unknown>) {
  const ui = useUIStore.getState();
  if (obj.expired) {
    ui.setPendingDesignPick(chatId, null);
    return;
  }
  const pick = toDesignPickInfo(obj);
  if (pick.confirmId && pick.options.length) ui.setPendingDesignPick(chatId, pick);
}

/**
 * Handle one `subagent_event`: attach the sub-agent's internal thinking/tool_call_delta/
 * tool_call/tool_result/content sub-steps under the call_subagent tool card that spawned it.
 *
 * Association prefers the backend-provided `parent_tool_id` (ActingToolCallIdMiddleware
 * guarantees accuracy); when missing, falls back to "the most recent call_subagent card".
 * Replaces the matched toolCall in place (new object, new subSteps array, new step object) so
 * the reference change triggers a React re-render.
 *
 * Returns true when grouped (the caller should refresh the bubble); false when there is no
 * matching parent card yet (normally the call_subagent tool_call arrives before its sub-events,
 * so this shouldn't happen).
 */
function applySubagentEvent(toolCalls: ToolCall[], eo: Record<string, unknown>): boolean {
  const norm = (v: unknown): string => (v == null ? '' : String(v));
  const parentId = norm(eo.parent_tool_id);
  // 事件自报父卡片工具名时按它回退（批量作业的进度贴的是 run_job，不是 call_subagent）
  const parentName = norm(eo.parent_tool_name) || 'call_subagent';
  let idx = -1;
  if (parentId) idx = toolCalls.findIndex((t) => norm(t?.id) === parentId);
  if (idx < 0) {
    for (let i = toolCalls.length - 1; i >= 0; i--) {
      if (toolCalls[i]?.name === parentName) { idx = i; break; }
    }
  }
  if (idx < 0) return false;

  // ── 批量作业进度：贴在 run_job 卡片头上的一行实时数字，不产生子步骤 ──
  // run_job(wait=true) 会把主对话阻塞几十分钟，其间没有任何新的工具调用或正文，
  // 卡片只剩一个转圈的菊花——这行是那段时间里唯一能证明"它在动"的东西。
  // 整行替换而不是追加：进度是同一件事的最新值，堆成流水账既没用又撑爆卡片。
  if (norm(eo.sub_type) === 'job_progress') {
    const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
    const total = num(eo.total);
    const settled = num(eo.settled);
    const failed = num(eo.failed);
    const note = total > 0
      ? t('作业进行中 {n}/{m}', { n: settled, m: total })
        + (failed > 0 ? t('（失败 {n}）', { n: failed }) : '')
      : t('正在建立工作项台账');
    toolCalls[idx] = { ...toolCalls[idx], progressNote: note };
    return true;
  }

  const parent = toolCalls[idx];
  const steps: SubagentStep[] = [...(parent.subSteps || [])];
  const subType = norm(eo.sub_type);
  const agentName = norm(eo.agent_name);

  // Merge-patch when a sub-tool step with the same toolId is hit, otherwise append (shared by tool_call and tool_result).
  const upsertToolStep = (tid: string, name: string, patch: Partial<SubagentStep>, newStatus: SubagentStep['status']) => {
    const si = tid ? steps.findIndex((x) => x.kind === 'tool' && x.toolId === tid) : -1;
    if (si >= 0) steps[si] = { ...steps[si], ...(name ? { name } : {}), ...patch };
    else steps.push({ kind: 'tool', toolId: tid || undefined, name: name || 'tool', status: newStatus, ...patch });
  };

  if (subType === 'tool_call') {
    const input = (eo.input === null || eo.input === undefined) ? undefined : eo.input;
    // No status included → an existing matched step keeps its status (success/error is not reset to running)
    upsertToolStep(norm(eo.tool_id), norm(eo.tool_name), input !== undefined ? { input } : {}, 'running');
  } else if (subType === 'tool_call_delta') {
    const delta = norm(eo.arguments_delta);
    if (delta) {
      const tid = norm(eo.tool_id);
      const si = tid ? steps.findIndex((x) => x.kind === 'tool' && x.toolId === tid) : -1;
      const inputText = (si >= 0 ? steps[si].inputText || '' : '') + delta;
      upsertToolStep(tid, norm(eo.tool_name), { inputText }, 'running');
    }
  } else if (subType === 'tool_result') {
    const status: SubagentStep['status'] = norm(eo.status) === 'error' ? 'error' : 'success';
    const patch: Partial<SubagentStep> = { status };
    if (eo.output !== null && eo.output !== undefined) patch.output = eo.output;
    upsertToolStep(norm(eo.tool_id), norm(eo.tool_name), patch, status);
  } else if (subType === 'thinking' || subType === 'content') {
    // 迟到思考尾并回前块（与主链路同一规则），避免思考与正文交错切碎
    appendSubagentStepDelta(steps, subType, norm(eo.delta));
  } else if (subType === 'error') {
    steps.push({ kind: 'content', text: '⚠ ' + (norm(eo.error) || 'error') });
  }
  // 'start' / 'end': only update subagentName, no sub-step produced

  toolCalls[idx] = { ...parent, subSteps: steps, ...(agentName ? { subagentName: agentName } : {}) };
  return true;
}

/** The minimal bubble-manipulation surface available to the onEvent hook — use it when a
 *  path-specific event (loop_error etc.) needs to write into the bubble; bypassing the
 *  processor to mutate the store directly is forbidden. */
export interface ChatStreamApi {
  /** Append body text (goes into full + the text segment) */
  appendText: (txt: string) => void;
  /** Whether there is already body text (loop_error etc. use this to decide whether to add a separating blank line) */
  hasText: () => boolean;
  /** Immediately flush the currently accumulated state into the bubble */
  refresh: () => void;
}

export interface ChatStreamOptions {
  /** Target chat — the stream writes into the assistant bubble at this chat's tail (a snapshot; switching chats has no effect) */
  chatId: string;
  /** Thinking mode (chatMode !== 'fast'): determines the <think> stripper's initial phase and re-arming after tools */
  enableThinking: boolean;
  /** Placeholder notice shown until the first real event arrives (confirm-then-continue scenarios etc.), never persisted */
  pendingNotice?: string;
  /** Path-specific event preprocessing (the autonomous loop's loop_*). Return true = handled, skip built-in dispatch. */
  onEvent?: (ev: Record<string, unknown>, api: ChatStreamApi) => boolean;
  /** 续接：以服务端持续刷新的那一行为基态（正文/工具卡/思考/段落照搬），流从它记下的
   *  event_offset 之后接着喂。这样重挂不必从头重放，Redis 事件流被裁掉多早的内容都无所谓。 */
  seedFrom?: ChatMessage;
}

export interface ChatStreamOutcome {
  /** Final body text (excluding thinking) */
  full: string;
  /** The assistant bubble's local ts (follow-up polling etc. locate the message by it) */
  placeholderTs: number;
  /** Backend message_id carried back by the meta event */
  metaMessageId?: string;
  /** Follow-up questions delivered directly within the stream */
  metaFollowUps: string[];
  /** Stream aborted by the user (AbortError) — the bubble has already wound down normally */
  aborted: boolean;
  /** A durable queued-input handoff committed by the backend at this run's boundary. */
  queuedRun?: QueuedRunHandoff;
}

/**
 * Consume one chat SSE stream (a fetch Response), rendering events uniformly into the assistant
 * bubble at the tail of `chatId`; when the stream ends ([DONE]/end/abort/exception) it finalizes
 * the message and returns the outcome. Non-abort exceptions are rethrown as-is after
 * finalization; the caller decides the notice copy.
 */
export async function processChatStream(resp: Response, opts: ChatStreamOptions): Promise<ChatStreamOutcome> {
  const { chatId, enableThinking, pendingNotice, onEvent } = opts;
  if (!resp.body) throw new Error('empty response body');

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let sseBuffer = '';
  let full = '';
  let streamEnded = false;
  let toolCalls: ToolCall[] = [];
  const thinking: { content: string; timestamp: number }[] = [];
  const segments: MessageSegment[] = [];
  let ontologyGovernance: OntologyGovernanceSummary | undefined;
  // Settlement runs after the stream closes, so all the closing frame can carry
  // is a skeleton marker; the real summary arrives via the settlement endpoint.
  let evolutionSummary: EvolutionSummary | undefined;
  let metaMessageId: string | undefined;
  let metaFollowUps: string[] = [];
  let queuedRun: ChatStreamOutcome['queuedRun'];
  let allCitations: CitationItem[] = [];
  // Workspace allowlist from the meta event. `null` means the agent
  // didn't pin (legacy behavior); an array means filter artifact cards
  // to only those file_ids.
  let metaWorkspaceFiles: string[] | null = null;
  // 服务端下发的本轮总耗时（毫秒）。有它就用它——本地用「占位气泡创建到现在」
  // 估出来的值把网络往返也算进去了，跟刷新后从历史读到的 duration_ms 对不上。
  let metaDurationMs: number | null = null;
  let compactionPending = false;
  let parseBuffer = '';
  let deferredThinkingText: DeferredThinkingTextFragment | undefined;
  let toolPending = false;
  let ontologySidebarAutoOpened = false;

  const ensureOntologyGovernance = (eventObj?: Record<string, unknown>) => {
    if (!ontologyGovernance) {
      ontologyGovernance = {
        governance_run_id: typeof eventObj?.governance_run_id === 'string' ? eventObj.governance_run_id : undefined,
        activations: [],
        gates: [],
        review: {},
      };
    } else if (!ontologyGovernance.governance_run_id && typeof eventObj?.governance_run_id === 'string') {
      ontologyGovernance = { ...ontologyGovernance, governance_run_id: eventObj.governance_run_id };
    }
    return ontologyGovernance;
  };
  let aborted = false;

  // ── <think>...</think> stripping state machine ──
  // Many models (qwen3 / DeepSeek family) inline their reasoning in the content stream. The
  // stripper cuts it into separate thinking segments so the bubble renders a collapsible
  // thinking block instead of visible body text.
  let thinkingPhaseActive = enableThinking;
  // Once a structured reasoning event is observed (e.g. DeepSeek v4 `reasoning_content`), pin
  // the stripper's phase to body — from then on content is no longer treated as buffered thinking.
  let structuredReasoning = false;
  // 隐式思考段追踪：思考模式下、未见任何 <think>/</think> 标签时，正文流被"假定为
  // 思考"塞进思考段（兼容吞开标签的部署）。结构化 reasoning 模型无思考输出时，这个
  // 假定会把整段正文误关进思考块。记录这些"假定"产生的段索引；一旦出现 </think>
  // 证实假定成立就清空；反之收到 structured_reasoning 标记 / 流结束仍无标签时，把
  // 它们重归类回正文。
  const implicitThinkSegIdxs = new Set<number>();
  let sawThinkCloseTag = false;

  const reclassifyImplicitThinking = (): boolean => {
    if (sawThinkCloseTag || implicitThinkSegIdxs.size === 0) return false;
    for (const i of implicitThinkSegIdxs) {
      const seg = segments[i];
      if (seg?.type === 'thinking' && seg.content) {
        segments[i] = { type: 'text', content: seg.content };
      }
    }
    implicitThinkSegIdxs.clear();
    // full 与 thinking 按重归类后的段重建（与 appendTextSeg 的顺序累加语义一致）
    full = segments.filter((s) => s.type === 'text').map((s) => s.content || '').join('');
    thinking.length = 0;
    for (const s of segments) {
      if (s.type === 'thinking' && s.content) thinking.push({ content: s.content, timestamp: Date.now() });
    }
    return true;
  };

  const getPartialTagLen = (text: string, tag: string): number => {
    for (let len = Math.min(tag.length - 1, text.length); len >= 1; len--) {
      if (tag.startsWith(text.slice(text.length - len))) return len;
    }
    return 0;
  };

  const ensureOntologyRevision = (source?: string) => {
    const governance = ensureOntologyGovernance();
    if (!governance.revision) {
      governance.revision = {
        status: 'pending',
        source,
        content: '',
        thinking: [],
        toolCalls: [],
      };
    } else if (source && !governance.revision.source) {
      governance.revision = { ...governance.revision, source };
    }
    return governance.revision;
  };

  const appendRevisionThinking = (content: string) => {
    if (!content) return;
    const revision = ensureOntologyRevision();
    const items = [...revision.thinking];
    const last = items[items.length - 1];
    if (last) items[items.length - 1] = { ...last, content: last.content + content };
    else items.push({ content, timestamp: Date.now() });
    revision.thinking = items;
  };

  const appendRevisionText = (content: string) => {
    if (!content) return;
    const revision = ensureOntologyRevision();
    revision.content += content;
  };

  const appendThinkContent = (content: string, isDelta: boolean) => {
    if (!content) return;
    const lastSeg = segments[segments.length - 1];
    const lastThink = isDelta && lastSeg?.type === 'thinking' ? lastSeg : null;
    if (lastThink) {
      segments[segments.length - 1] = {
        ...lastThink,
        content: (lastThink.content || '') + content,
      };
      if (thinking.length > 0) {
        const lastThinking = thinking[thinking.length - 1];
        thinking[thinking.length - 1] = {
          ...lastThinking,
          content: lastThinking.content + content,
        };
      } else {
        thinking.push({ content, timestamp: Date.now() });
      }
    } else {
      segments.push({ type: 'thinking', content });
      thinking.push({ content, timestamp: Date.now() });
    }
  };

  const appendLateStructuredThinkContent = (content: string) => {
    if (!appendThinkingContentBeforeTrailingText(segments, content)) {
      appendThinkContent(content, true);
      return;
    }
    const lastThinking = thinking[thinking.length - 1];
    if (lastThinking) {
      thinking[thinking.length - 1] = {
        ...lastThinking,
        content: lastThinking.content + content,
      };
    }
  };

  const appendTextSeg = (text: string) => {
    if (!text) return;
    full += text;
    deferredThinkingText = appendStreamTextSegment(segments, text, deferredThinkingText);
  };

  const replaceAnswerText = (text: string) => {
    // Keep reasoning and tool chronology, but replace every draft text
    // segment with the committee-reviewed final answer. This event arrives
    // only when the review actually changed the draft.
    full = text;
    deferredThinkingText = undefined;
    for (let i = segments.length - 1; i >= 0; i--) {
      if (segments[i].type === 'text') segments.splice(i, 1);
    }
    if (text) segments.push({ type: 'text', content: text });
  };

  /** Streaming <think>/<\/think> splitter: buffers half-cut tags across deltas and routes
   *  content into thinking or text segments. An explicit <think> open tag re-enters the
   *  thinking phase; an orphan </think> (model omitted the open tag) classifies the buffer
   *  preceding it as thinking. */
  const processTextChunk = (chunk: string) => {
    parseBuffer += chunk;
    while (parseBuffer.length > 0) {
      if (thinkingPhaseActive) {
        const openIdx = parseBuffer.indexOf('<think>');
        const closeIdx = parseBuffer.indexOf('</think>');
        // A redundant open tag while already in the thinking phase: drop the tag itself; text before it is still thinking.
        if (openIdx >= 0 && (closeIdx === -1 || openIdx < closeIdx)) {
          if (openIdx > 0) appendThinkContent(parseBuffer.slice(0, openIdx), true);
          parseBuffer = parseBuffer.slice(openIdx + 7);
          continue;
        }
        if (closeIdx === -1) {
          const partialLen = getPartialTagLen(parseBuffer, '</think>');
          const safeLen = parseBuffer.length - partialLen;
          if (safeLen > 0) {
            appendThinkContent(parseBuffer.slice(0, safeLen), true);
            // 该思考内容是"假定"出来的（本轮还没见到任何 think 标签）——记录段索引
            if (!sawThinkCloseTag) implicitThinkSegIdxs.add(segments.length - 1);
            parseBuffer = parseBuffer.slice(safeLen);
          }
          break;
        }
        if (closeIdx > 0) appendThinkContent(parseBuffer.slice(0, closeIdx), true);
        parseBuffer = parseBuffer.slice(closeIdx + 8);
        thinkingPhaseActive = false;
        // 出现真实 </think>：假定成立，此前的隐式思考段确属思考
        sawThinkCloseTag = true;
        implicitThinkSegIdxs.clear();
      } else {
        const openIdx = parseBuffer.indexOf('<think>');
        const closeIdx = parseBuffer.indexOf('</think>');
        // Orphan close tag (no paired <think>): the model omitted the open tag (common after
        // tool calls, in fast mode, or after a structured reasoning event pinned the phase to
        // body). Everything before the close tag is reasoning, not body text.
        if (closeIdx >= 0 && (openIdx === -1 || closeIdx < openIdx)) {
          if (closeIdx > 0) appendThinkContent(parseBuffer.slice(0, closeIdx), true);
          parseBuffer = parseBuffer.slice(closeIdx + 8);
          sawThinkCloseTag = true;
          implicitThinkSegIdxs.clear();
          continue;
        }
        if (openIdx === -1) {
          const partialLen = Math.max(
            getPartialTagLen(parseBuffer, '<think>'),
            getPartialTagLen(parseBuffer, '</think>'),
          );
          const safeLen = parseBuffer.length - partialLen;
          if (safeLen > 0) {
            appendTextSeg(parseBuffer.slice(0, safeLen));
            parseBuffer = parseBuffer.slice(safeLen);
          }
          break;
        }
        if (openIdx > 0) appendTextSeg(parseBuffer.slice(0, openIdx));
        parseBuffer = parseBuffer.slice(openIdx + 7);
        thinkingPhaseActive = true;
      }
    }
  };

  const normalizeToolId = (value: unknown): string | undefined => {
    if (typeof value !== 'string') return undefined;
    const id = value.trim();
    return id.length > 0 ? id : undefined;
  };

  const getEventToolId = (obj: Record<string, unknown>) =>
    normalizeToolId(obj.id) || normalizeToolId(obj.tool_call_id) || normalizeToolId(obj.call_id) || normalizeToolId(obj.tool_id);

  const getEventToolRawName = (obj: Record<string, unknown>) => {
    const candidates = [obj.name, obj.tool_name, obj.tool, obj.title];
    for (const candidate of candidates) {
      if (typeof candidate === 'string' && candidate.trim()) return stripMcpToolPrefix(candidate.trim());
    }
    return undefined;
  };

  const getEventToolDisplayName = (obj: Record<string, unknown>) => {
    if (typeof obj.tool_display_name === 'string' && obj.tool_display_name.trim()) {
      // When the backend can't find a Chinese display name it falls back to the raw tool name;
      // an mcp__ prefix means it's just an echo — discard it and let the frontend's
      // TOOL_NAME_OVERRIDES / toolDisplayNames lookup chain take over.
      if (obj.tool_display_name.trim().startsWith('mcp__')) return undefined;
      let displayName = obj.tool_display_name.trim();
      if (typeof obj.subagent_name === 'string' && obj.subagent_name.trim()) {
        displayName += `：${obj.subagent_name.trim()}`;
      }
      return displayName;
    }
    return undefined;
  };

  const findLastRunningToolIndex = (name?: string) => {
    for (let i = toolCalls.length - 1; i >= 0; i--) {
      if (toolCalls[i].status !== 'running') continue;
      if (name && toolCalls[i].name !== name) continue;
      return i;
    }
    return -1;
  };

  const findToolCallIndex = (obj: Record<string, unknown>) => {
    const eventToolId = getEventToolId(obj);
    if (eventToolId) {
      const directIndex = toolCalls.findIndex((tool) => normalizeToolId(tool.id) === eventToolId);
      if (directIndex >= 0) return directIndex;
    }
    const eventToolName = getEventToolRawName(obj);
    if (eventToolName) {
      const byNameIndex = findLastRunningToolIndex(eventToolName);
      if (byNameIndex >= 0) return byNameIndex;
    }
    // Last resort — bind to whatever is still running — only for events that
    // carry no tool_id at all. An id that matched nothing means the result
    // belongs to a card we never created (a tool_call event we never got); with
    // tools running in parallel, grabbing an unrelated running card would file
    // this output under the wrong tool and hide the real call entirely.
    if (eventToolId) return -1;
    return findLastRunningToolIndex();
  };

  const finalizeRunningTools = (status: 'success' | 'error' = 'success') => {
    let changed = false;
    toolCalls = toolCalls.map((tool) => {
      if (tool.status !== 'running') return tool;
      changed = true;
      return { ...tool, status };
    });
    return changed;
  };

  const appendArtifactsToStreamToolCalls = (artifacts: unknown[]) => {
    if (!Array.isArray(artifacts) || artifacts.length === 0) return false;
    const existingFileIds = new Set<string>();
    for (const tool of toolCalls) {
      if (!tool?.output || typeof tool.output !== 'object') continue;
      const fileId = (tool.output as Record<string, unknown>).file_id;
      if (typeof fileId === 'string' && fileId.trim()) existingFileIds.add(fileId.trim());
    }
    let changed = false;
    let latestHtml: { file_id: string; name: string; url: string; mime_type?: string; size?: number } | null = null;
    for (const artifact of artifacts) {
      const output = normalizeArtifactOutput(artifact);
      if (!output) continue;
      const fileId = String(output.file_id);
      if (existingFileIds.has(fileId)) continue;
      existingFileIds.add(fileId);
      toolCalls.push({ id: `artifact_${fileId}`, name: t('附件'), output, status: 'success', timestamp: Date.now() });
      segments.push({ type: 'tool', toolIndex: toolCalls.length - 1 });
      changed = true;
      // Auto-open Canvas when an HTML artifact arrives (Claude-style live preview).
      // Track the last HTML in the batch and open it after the loop.
      const name = String(output.name || '').toLowerCase();
      const mime = String(output.mime_type || '').toLowerCase();
      const isHtml = name.endsWith('.html') || name.endsWith('.htm') || mime === 'text/html';
      if (isHtml) {
        latestHtml = {
          file_id: fileId,
          name: String(output.name || 'preview.html'),
          url: String(output.url || ''),
          mime_type: typeof output.mime_type === 'string' ? output.mime_type : undefined,
          size: typeof output.size === 'number' ? output.size : undefined,
        };
      }
    }
    if (latestHtml && latestHtml.url && canAutoOpenCanvas()) {
      const canvas = useCanvasStore.getState();
      // Don't steal focus from a different file the user is actively viewing —
      // only auto-open if Canvas is closed or already showing this same artifact.
      if (!canvas.isOpen || !canvas.artifact || canvas.artifact.file_id === latestHtml.file_id) {
        canvas.openCanvas({ ...latestHtml, chat_id: chatId });
      }
    }
    return changed;
  };

  let placeholderTs = Date.now();
  const seed = opts.seedFrom;
  if (seed) {
    placeholderTs = seed.ts;
    metaMessageId = seed.messageId;
    full = seed.content || '';
    toolCalls = (seed.toolCalls || []).map((tool) => ({ ...tool }));
    thinking.push(...(seed.thinking || []).map((block) => ({
      content: block.content,
      timestamp: block.timestamp ?? seed.ts,
    })));
    segments.push(...(seed.segments || []));
    // 基态里已有正文：思考阶段早就过了，剥离器等下一个 <think> 再重新武装。
    if (full) thinkingPhaseActive = false;
  }
  const autoOpenOntologySidebar = () => {
    if (ontologySidebarAutoOpened) return;
    ontologySidebarAutoOpened = true;
    // A background stream must not replace the panel in the chat the user is
    // currently reading. The result remains available from its message entry.
    if (useChatStore.getState().currentChatId !== chatId) return;
    if (useCatalogStore.getState().panel !== 'chat') return;
    // 移动端不自动弹（整屏覆盖），评审结论仍可从消息里的入口打开。
    if (!canAutoOpenCanvas()) return;
    useCanvasStore.getState().openOntology({ chatId, messageTs: placeholderTs });
  };
  /** 本轮气泡在列表里的位置：先按服务端 message_id，还没拿到时按本地占位 ts。 */
  const findOwnBubble = (msgs: ChatMessage[]): number => {
    const byId = metaMessageId
      ? msgs.findIndex((m) => m.role === 'assistant' && m.messageId === metaMessageId)
      : -1;
    return byId >= 0 ? byId : msgs.findIndex((m) => m.role === 'assistant' && m.ts === placeholderTs);
  };

  const commitUpdate = (
    streaming: boolean,
    cits?: CitationItem[],
    persistedMessageId?: string,
  ) => {
    useChatStore.getState().updateStore((prev) => {
      const c = prev.chats[chatId];
      const msgs = [...(c?.messages || [])];
      // While the model hasn't produced any real content yet (MiniMax may buffer the whole
      // turn), show the placeholder notice instead of an empty bubble. The placeholder never
      // enters full/segments, so it is never persisted.
      const body = (
        !full && streaming && toolCalls.length === 0 && thinking.length === 0
          ? (pendingNotice || '')
          : full
      );
      const isMd = body.includes('\n') || body.includes('```') || body.includes('**') || /^\s*#\s/m.test(body);
      const updatedMsg: Partial<ChatMessage> & { content: string; isMarkdown: boolean; isStreaming: boolean } = {
        content: body,
        isMarkdown: isMd,
        toolCalls: toolCalls.length > 0 ? [...toolCalls] : undefined,
        thinking: thinking.length > 0 ? [...thinking] : undefined,
        evolution: evolutionSummary,
        ontologyGovernance: ontologyGovernance
          ? {
              ...ontologyGovernance,
              activations: [...ontologyGovernance.activations],
              gates: [...ontologyGovernance.gates],
              review: { ...ontologyGovernance.review },
              revision: ontologyGovernance.revision
                ? {
                    ...ontologyGovernance.revision,
                    thinking: [...ontologyGovernance.revision.thinking],
                    toolCalls: [...ontologyGovernance.revision.toolCalls],
                  }
                : undefined,
            }
          : undefined,
        segments: segments.length > 0 ? [...segments] : undefined,
        isStreaming: streaming,
        toolPending: streaming && toolPending,
        // Persisted activity stamp — anchors the "正在准备调用工具…" timer so
        // it survives a session switch / refresh remount (see useStallDetector).
        lastActivityTs: Date.now(),
      };
      if (persistedMessageId) updatedMsg.messageId = persistedMessageId;
      if (!streaming) {
        updatedMsg.durationMs = metaDurationMs ?? (Date.now() - placeholderTs);
        updatedMsg.inFlight = undefined;
      }
      if (cits !== undefined) updatedMsg.citations = cits.length > 0 ? cits : undefined;
      if (metaFollowUps.length > 0) updatedMsg.followUpQuestions = metaFollowUps;

      // 身份是服务端 message_id（run_started 第一帧就有）；还没拿到时退回本地占位 ts。
      const idx = findOwnBubble(msgs);
      if (idx >= 0) {
        msgs[idx] = { ...msgs[idx], ...updatedMsg };
      } else {
        msgs.push({ role: 'assistant', ts: placeholderTs, ...updatedMsg });
      }
      // Don't bump updatedAt / reorder on every SSE chunk — otherwise when two chats stream
      // simultaneously, the sidebar's updatedAt sort keeps lifting each to the top in turn and
      // the list starts bouncing. The initiator already moved the chat to the front, and the
      // final update at stream end bumps it once more; the in-between just needs to stay stable.
      const nextChat: ChatItem = { ...(c as ChatItem), messages: msgs };
      return { chats: { ...prev.chats, [chatId]: nextChat }, order: prev.order };
    });
  };

  // 流式增量的合并写。每个 SSE 事件都直接落 store 时，气泡每长一点就要把**整篇**
  // 正文重新解析成 markdown、重建整棵 DOM 再 diff 回去 —— 单次开销随正文长度线性
  // 增长，一轮回答累计下来是平方级。实测 95KB 的回答光 markdown 解析就要 16s 主线程
  // （3228 个增量 × 逐次全量解析），叠加 DOM 重建后标签页直接被撑爆，用户看到的就是
  // "输出太长页面崩溃"。这里把流式写入按时间窗合并，窗口随正文长度自适应放大；
  // 终态（streaming=false）永远立即写，收尾结果与合并前完全一致。
  let pendingStreamingUpdate: { cits?: CitationItem[]; persistedMessageId?: string } | null = null;
  let streamingUpdateTimer: ReturnType<typeof setTimeout> | null = null;
  let lastStreamingCommitAt = 0;

  /** 正文越长，全量重排一次越贵，合并窗口就越宽——但**上限必须保持在肉眼仍读作
   *  流式输出的范围内**，不能为了给极端长文留余量把常规观感一起牺牲掉。
   *  5 万字以内维持 50ms（≈20 次/秒，与逐字输出无异）；再长按长度线性放宽，
   *  封顶 250ms（≈4 次/秒，仍是一段段往外流，不是整段蹦出来）。 */
  const streamingCommitDelay = () => Math.min(250, Math.max(50, Math.round(full.length / 1000)));

  const cancelStreamingUpdate = () => {
    if (streamingUpdateTimer != null) {
      clearTimeout(streamingUpdateTimer);
      streamingUpdateTimer = null;
    }
    pendingStreamingUpdate = null;
  };

  const flushStreamingUpdate = () => {
    if (streamingUpdateTimer != null) {
      clearTimeout(streamingUpdateTimer);
      streamingUpdateTimer = null;
    }
    const pending = pendingStreamingUpdate;
    pendingStreamingUpdate = null;
    if (!pending) return;
    lastStreamingCommitAt = Date.now();
    commitUpdate(true, pending.cits, pending.persistedMessageId);
  };

  const appendOrUpdate = (
    streaming: boolean,
    cits?: CitationItem[],
    persistedMessageId?: string,
  ) => {
    if (!streaming) {
      // 终态先把待写的增量丢掉：它携带的是同一份可变状态的旧快照，
      // 而下面这次写入本来就带着最新的全量内容。
      cancelStreamingUpdate();
      lastStreamingCommitAt = Date.now();
      commitUpdate(false, cits, persistedMessageId);
      return;
    }
    // 参数按"最后给出的非空值"合并：合并窗口内多次调用只有一次落盘，
    // 但引用列表 / message_id 这类附带信息不能被后来的裸调用抹掉。
    pendingStreamingUpdate = {
      cits: cits !== undefined ? cits : pendingStreamingUpdate?.cits,
      persistedMessageId: persistedMessageId ?? pendingStreamingUpdate?.persistedMessageId,
    };
    if (streamingUpdateTimer != null) return;
    const delay = streamingCommitDelay();
    const elapsed = Date.now() - lastStreamingCommitAt;
    // 首帧与空闲后的第一帧立即出，不给用户"迟迟不吐字"的观感。
    if (elapsed >= delay) {
      flushStreamingUpdate();
      return;
    }
    streamingUpdateTimer = setTimeout(flushStreamingUpdate, delay - elapsed);
  };

  const applySteerBoundary = (eventObj: Record<string, unknown>) => {
    // Finish any half-buffered inline-reasoning token before freezing the
    // current assistant bubble. The next model iteration starts a fresh bubble.
    if (parseBuffer) {
      if (thinkingPhaseActive && sawThinkCloseTag) appendThinkContent(parseBuffer, true);
      else appendTextSeg(parseBuffer);
      parseBuffer = '';
    }
    reclassifyImplicitThinking();
    deferredThinkingText = restoreDeferredThinkingTextFragment(segments, deferredThinkingText);
    // 冻结气泡前收尾：工具卡/思考块不留在最终答案之后（与历史重建同一规则）
    liftTrailingSegmentsAboveFinalText(segments);

    const previousAssistantMessageId = typeof eventObj.previous_assistant_message_id === 'string'
      ? eventObj.previous_assistant_message_id
      : undefined;
    const nextAssistantMessageId = typeof eventObj.next_assistant_message_id === 'string'
      ? eventObj.next_assistant_message_id
      : undefined;
    const steerMessageId = typeof eventObj.message_id === 'string'
      ? eventObj.message_id
      : undefined;
    const steerMessage = typeof eventObj.message === 'string' ? eventObj.message.trim() : '';
    const hasAssistantOutput = full.length > 0
      || toolCalls.length > 0
      || thinking.length > 0
      || segments.length > 0;

    if (hasAssistantOutput) {
      appendOrUpdate(false, allCitations, previousAssistantMessageId);
    }

    let steerMessageTs = Date.now();
    useChatStore.getState().updateStore((prev) => {
      const chat = prev.chats[chatId];
      if (!chat) return prev;
      const messages = [...chat.messages];
      let assistantIndex = messages.findIndex(
        (item) => item.role === 'assistant' && item.ts === placeholderTs,
      );
      if (!hasAssistantOutput && assistantIndex >= 0) {
        messages.splice(assistantIndex, 1);
        assistantIndex -= 1;
      }

      const existingUserIndex = steerMessageId
        ? messages.findIndex((item) => item.messageId === steerMessageId)
        : -1;
      if (existingUserIndex >= 0) {
        steerMessageTs = messages[existingUserIndex].ts;
      } else if (steerMessage) {
        steerMessageTs = Math.max(Date.now(), placeholderTs + 1);
        const userMessage: ChatMessage = {
          role: 'user',
          content: steerMessage,
          isMarkdown: false,
          ts: steerMessageTs,
          messageId: steerMessageId,
        };
        messages.splice(assistantIndex >= 0 ? assistantIndex + 1 : messages.length, 0, userMessage);
      }

      return {
        ...prev,
        chats: {
          ...prev.chats,
          [chatId]: { ...chat, messages, updatedAt: Date.now() },
        },
      };
    });

    // The queue card has now become a real chronological user message.
    useChatStore.getState().setQueuedMessage(chatId, null);

    full = '';
    toolCalls = [];
    thinking.length = 0;
    segments.length = 0;
    ontologyGovernance = undefined;
    evolutionSummary = undefined;
    metaMessageId = nextAssistantMessageId;
    metaFollowUps = [];
    metaDurationMs = null;
    allCitations = [];
    metaWorkspaceFiles = null;
    parseBuffer = '';
    deferredThinkingText = undefined;
    toolPending = false;
    implicitThinkSegIdxs.clear();
    sawThinkCloseTag = false;
    thinkingPhaseActive = enableThinking && !structuredReasoning;
    placeholderTs = Math.max(Date.now(), steerMessageTs + 1);
    appendOrUpdate(true);
  };

  const hookApi: ChatStreamApi = {
    appendText: (txt: string) => appendTextSeg(txt),
    hasText: () => full.length > 0,
    refresh: () => {
      appendOrUpdate(true, allCitations);
      flushStreamingUpdate();
    },
  };

  appendOrUpdate(true);

  const handleSsePayload = (payload: string) => {
    const trimmedPayload = payload.trim();
    if (!trimmedPayload) return;
    if (trimmedPayload === '[DONE]') {
      if (finalizeRunningTools()) appendOrUpdate(true);
      streamEnded = true;
      return;
    }

    let textChunk = '';
    let parsed = false;
    try {
      const obj = JSON.parse(trimmedPayload);
      parsed = true;
      if (typeof obj === 'string') {
        textChunk = obj;
      } else if (obj && typeof obj === 'object') {
        const eventObj = obj as Record<string, unknown>;
        const eventType = typeof obj.type === 'string' ? obj.type : '';

        // Path-specific events (autonomous loop loop_* etc.) go to the hook first
        if (onEvent && onEvent(eventObj, hookApi)) return;

        if (eventType === 'run_started') {
          const runId = typeof eventObj.run_id === 'string' ? eventObj.run_id : '';
          const messageId = typeof eventObj.message_id === 'string' ? eventObj.message_id : '';
          if (runId) {
            // 有上限地记一笔：单页会话再长也不该让这个集合无限涨
            if (_seenRuns.size > 200) _seenRuns.clear();
            _seenRuns.add(runId);
            useChatStore.getState().setActiveRun(chatId, { runId, messageId });
          }
          if (messageId) {
            // 第一帧就认领服务端身份。后端在接纳轮次时已经建好了这一行，如果历史里
            // 已经把它渲染出来（重载/刷新后跟随），就直接接管那个气泡，不再另起一个。
            metaMessageId = messageId;
            useChatStore.getState().updateStore((prev) => {
              const c = prev.chats[chatId];
              if (!c) return prev;
              const msgs = c.messages || [];
              const owned = msgs.find((m) => m.role === 'assistant' && m.messageId === messageId);
              if (!owned || owned.ts === placeholderTs) return prev;
              const withoutPlaceholder = msgs.filter(
                (m) => !(m.role === 'assistant' && m.ts === placeholderTs && !m.messageId),
              );
              placeholderTs = owned.ts;
              return { ...prev, chats: { ...prev.chats, [chatId]: { ...c, messages: withoutPlaceholder } } };
            });
            appendOrUpdate(true, undefined, messageId);
          }
          return;
        }
        if (eventType === 'steer_applied') {
          applySteerBoundary(eventObj);
          return;
        }
        if (eventType === 'queued_run_started') {
          queuedRun = parseQueuedRunHandoff(eventObj);
          return;
        }
        if (eventType === 'vision_progress') {
          // 视觉桥在模型开口前先把图转成文字证据，这段是纯网络等待。不报出来的话，
          // 界面上只有一个笼统的「深度拥抱中」在走秒，用户不知道系统在干什么。
          const running = eventObj.status === 'running';
          const count = typeof eventObj.count === 'number' ? eventObj.count : 1;
          useChatStore.getState().setVisionReading(chatId, running ? count : 0);
          return;
        }
        if (eventType === 'compaction_notice') {
          // Earlier context was compacted in the background after the previous turn ended;
          // the backend notifies once in this turn's first frame
          // → ChatArea shows a dismissible notice bar
          const chatStore = useChatStore.getState();
          chatStore.setCompactionNotice(chatId);
          const contextCompaction = parseContextCompactionState(eventObj.context_compaction);
          if (contextCompaction) chatStore.setContextCompaction(chatId, contextCompaction);
          return;
        }
        if (eventType === 'context_usage') {
          const contextUsage = parseContextUsageSnapshot(eventObj);
          if (contextUsage) useChatStore.getState().setContextUsage(chatId, contextUsage);
          return;
        }
        if (eventType === 'end') {
          if (finalizeRunningTools()) appendOrUpdate(true);
          streamEnded = true;
          return;
        }
        if (eventType === 'error') {
          const streamError = typeof obj.error === 'string' ? obj.error : t('流式响应异常');
          // A server error frame is terminal for this run. Suspended question
          // tools are cancelled with it, but their resolved signal may not be
          // drained before task cancellation writes the terminal frame.
          const ui = useUIStore.getState();
          for (const request of ui.pendingUserQuestions[chatId] ?? []) {
            ui.resolvePendingUserQuestion(chatId, request.requestId);
          }
          if (ontologyGovernance?.review.status === 'running') {
            ontologyGovernance = {
              ...ontologyGovernance,
              review: {
                ...ontologyGovernance.review,
                status: 'failed',
                verdict: 'escalate',
                revised: false,
                error: streamError,
                feedback: [t('自动评审未完成，原文已保留。')],
                manual_review: {
                  required: true,
                  title: t('领域本体人工复核'),
                  summary: t('自动评审未完成，原文已保留，请重新发起评审或人工核对。'),
                  items: [],
                  actions: [],
                },
              },
            };
            appendOrUpdate(false, allCitations);
          }
          throw new Error(streamError);
        }

        const ontologyRevisionTool = eventObj.scope === 'ontology_revision';

        if (eventType === 'tool_pending' && ontologyRevisionTool) {
          const revision = ensureOntologyRevision();
          revision.status = 'streaming';
          revision.toolPending = true;
          appendOrUpdate(true, allCitations);
          return;
        }

        if (eventType === 'tool_pending') {
          if (!toolPending) {
            toolPending = true;
            appendOrUpdate(true);
          }
          return;
        }

        if (toolPending && eventType !== 'heartbeat') {
          toolPending = false;
          appendOrUpdate(true);
        }

        if (eventType === 'ontology_repair') {
          const revision = ensureOntologyRevision(
            typeof eventObj.source === 'string' ? eventObj.source : undefined,
          );
          revision.status = eventObj.status === 'completed' ? 'completed' : 'streaming';
          if (eventObj.status === 'started') autoOpenOntologySidebar();
          if (eventObj.status === 'started' || eventObj.status === 'completed') {
            revision.toolPending = false;
          }
          if (typeof eventObj.tool_calls === 'number') revision.toolCallCount = eventObj.tool_calls;
          appendOrUpdate(true, allCitations);
          return;
        }

        if (eventType === 'ontology_revision_thinking') {
          const content = String(eventObj.delta || eventObj.content || '');
          if (content) appendRevisionThinking(content);
          ensureOntologyRevision().toolPending = false;
          appendOrUpdate(true, allCitations);
          return;
        }

        if (eventType === 'ontology_revision') {
          const content = String(eventObj.delta || eventObj.content || '');
          // The backend has already separated pre-wrapper reasoning from the
          // <ontology_revision> body, so every delta can be rendered directly.
          // Sending it through the outer <think> state machine would buffer the
          // whole candidate as hidden reasoning when no </think> tag follows.
          const revision = ensureOntologyRevision();
          revision.status = 'streaming';
          revision.toolPending = false;
          autoOpenOntologySidebar();
          if (content) appendRevisionText(content);
          appendOrUpdate(true, allCitations);
          return;
        }

        if (ontologyRevisionTool && (eventType === 'tool_use' || eventType === 'tool_call_start' || eventType === 'tool_call' || eventType === 'tool_start')) {
          const revision = ensureOntologyRevision();
          revision.toolPending = false;
          const eventToolId = getEventToolId(eventObj);
          const existingIndex = eventToolId
            ? revision.toolCalls.findIndex((tool) => normalizeToolId(tool.id) === eventToolId)
            : -1;
          const toolInput = eventObj.input ?? eventObj.args ?? eventObj.tool_args ?? eventObj.arguments;
          const rawName = getEventToolRawName(eventObj) || t('工具调用');
          const displayName = getEventToolDisplayName(eventObj);
          if (existingIndex >= 0) {
            revision.toolCalls[existingIndex] = {
              ...revision.toolCalls[existingIndex],
              input: toolInput,
              status: 'running',
            };
          } else {
            revision.toolCalls = [...revision.toolCalls, {
              id: eventToolId || `ontology_tool_${Date.now()}_${revision.toolCalls.length}`,
              name: rawName,
              displayName,
              input: toolInput,
              status: 'running',
              timestamp: Date.now(),
              scope: 'ontology_revision',
            }];
          }
          appendOrUpdate(true, allCitations);
          return;
        }

        if (ontologyRevisionTool && eventType === 'tool_call_delta') {
          const revision = ensureOntologyRevision();
          revision.toolPending = false;
          const eventToolId = getEventToolId(eventObj);
          const delta = typeof eventObj.arguments_delta === 'string' ? eventObj.arguments_delta : '';
          const index = eventToolId
            ? revision.toolCalls.findIndex((tool) => normalizeToolId(tool.id) === eventToolId)
            : -1;
          if (index < 0) {
            revision.toolCalls = [...revision.toolCalls, {
              id: eventToolId || `ontology_tool_${Date.now()}_${revision.toolCalls.length}`,
              name: getEventToolRawName(eventObj) || t('工具调用'),
              inputText: delta,
              status: 'running',
              timestamp: Date.now(),
              scope: 'ontology_revision',
            }];
          } else if (delta) {
            revision.toolCalls[index] = {
              ...revision.toolCalls[index],
              inputText: (revision.toolCalls[index].inputText || '') + delta,
              status: 'running',
            };
          }
          appendOrUpdate(true, allCitations);
          return;
        }

        if (ontologyRevisionTool && (eventType === 'tool_result' || eventType === 'tool_end')) {
          const revision = ensureOntologyRevision();
          revision.toolPending = false;
          const eventToolId = getEventToolId(eventObj);
          const toolName = getEventToolRawName(eventObj);
          let index = eventToolId
            ? revision.toolCalls.findIndex((tool) => normalizeToolId(tool.id) === eventToolId)
            : -1;
          if (index < 0 && toolName) {
            index = revision.toolCalls.findIndex((tool) => tool.name === toolName && tool.status === 'running');
          }
          const output = eventObj.output ?? eventObj.result;
          if (index >= 0) {
            revision.toolCalls[index] = {
              ...revision.toolCalls[index],
              output,
              status: obj.error ? 'error' : 'success',
            };
          } else {
            revision.toolCalls = [...revision.toolCalls, {
              id: eventToolId,
              name: toolName || t('工具调用'),
              output,
              status: obj.error ? 'error' : 'success',
              timestamp: Date.now(),
              scope: 'ontology_revision',
            }];
          }
          if (Array.isArray(eventObj.citations)) {
            allCitations = [...allCitations, ...(eventObj.citations as CitationItem[])];
          }
          appendOrUpdate(true, allCitations);
          return;
        }

        if (ontologyRevisionTool && eventType === 'subagent_event') {
          const revision = ensureOntologyRevision();
          revision.toolPending = false;
          if (applySubagentEvent(revision.toolCalls, eventObj)) {
            if (Array.isArray(eventObj.citations)) {
              allCitations = [...allCitations, ...(eventObj.citations as CitationItem[])];
            }
            appendOrUpdate(true, allCitations);
          }
          return;
        }

        if (eventType === 'tool_use' || eventType === 'tool_call_start' || eventType === 'tool_call' || eventType === 'tool_start') {
          const eventToolId = getEventToolId(eventObj);
          const existingIndex = eventToolId ? toolCalls.findIndex((tool) => normalizeToolId(tool.id) === eventToolId) : -1;
          const toolInput = eventObj.input ?? eventObj.args ?? eventObj.tool_args ?? eventObj.arguments;
          const rawName = getEventToolRawName(eventObj);
          const displayName = getEventToolDisplayName(eventObj);
          let activeToolId = eventToolId;
          let activeToolName = rawName;
          if (existingIndex >= 0) {
            const existing = toolCalls[existingIndex];
            toolCalls[existingIndex] = { ...existing, name: rawName || existing.name, displayName: displayName || existing.displayName, input: toolInput ?? existing.input, status: 'running' };
            activeToolId = normalizeToolId(toolCalls[existingIndex].id);
            activeToolName = toolCalls[existingIndex].name;
          } else {
            activeToolId = eventToolId || `tool_${Date.now()}_${toolCalls.length}`;
            toolCalls.push({ id: activeToolId, name: rawName || t('工具调用'), displayName, input: toolInput, status: 'running', timestamp: Date.now() });
            deferredThinkingText = deferThinkingTextFragmentBeforeTool(
              segments,
              enableThinking,
              deferredThinkingText,
            );
            segments.push({ type: 'tool', toolIndex: toolCalls.length - 1 });
          }
          const pendingCanvas = findAutoCanvas(activeToolName);
          if (pendingCanvas && canOpenPluginCanvasForChat(chatId)) {
            useCanvasStore.getState().openPluginView({
              chatId,
              slug: pendingCanvas.slug,
              canvasId: pendingCanvas.id,
              toolId: activeToolId,
              toolName: activeToolName,
              title: canvasTabTitle(pendingCanvas, toolInput),
              status: 'loading',
            });
          }
          appendOrUpdate(true);
          return;
        }

        if (eventType === 'tool_call_delta') {
          const eventToolId = getEventToolId(eventObj);
          const delta = typeof eventObj.arguments_delta === 'string' ? eventObj.arguments_delta : '';
          const index = eventToolId
            ? toolCalls.findIndex((tool) => normalizeToolId(tool.id) === eventToolId)
            : -1;
          if (index < 0) {
            toolCalls.push({
              id: eventToolId || `tool_${Date.now()}_${toolCalls.length}`,
              name: getEventToolRawName(eventObj) || t('工具调用'),
              inputText: delta,
              status: 'running',
              timestamp: Date.now(),
            });
            deferredThinkingText = deferThinkingTextFragmentBeforeTool(
              segments,
              enableThinking,
              deferredThinkingText,
            );
            segments.push({ type: 'tool', toolIndex: toolCalls.length - 1 });
          } else if (delta) {
            toolCalls[index] = {
              ...toolCalls[index],
              inputText: (toolCalls[index].inputText || '') + delta,
              status: 'running',
            };
          }
          appendOrUpdate(true);
          return;
        }

        if (eventType === 'tool_result' || eventType === 'tool_end') {
          const toolIndex = findToolCallIndex(eventObj);
          const status: ToolCall['status'] = obj.error || eventObj.status === 'error'
            ? 'error'
            : eventObj.status === 'interrupted'
              ? 'interrupted'
              : 'success';
          const output = eventObj.output ?? eventObj.result;

          let resultDisplayName: string | undefined;
          if (typeof obj.subagent_name === 'string' && obj.subagent_name.trim()) {
            resultDisplayName = t('调用智能体：{name}', { name: obj.subagent_name.trim() });
          }

          let confirmToolName = '';
          if (toolIndex >= 0) {
            const existing = toolCalls[toolIndex];
            confirmToolName = existing.name;
            toolCalls[toolIndex] = { ...existing, output: output ?? existing.output, status, ...(resultDisplayName ? { displayName: resultDisplayName } : {}) };
          } else {
            confirmToolName = getEventToolRawName(eventObj) || t('工具调用');
            toolCalls.push({ id: getEventToolId(eventObj) || `tool_${Date.now()}_${toolCalls.length}`, name: confirmToolName, displayName: resultDisplayName || getEventToolDisplayName(eventObj), output, status, timestamp: Date.now() });
            segments.push({ type: 'tool', toolIndex: toolCalls.length - 1 });
          }
          maybeRefreshCatalogAfterTool(confirmToolName, status || 'success');
          const completedCanvas = findAutoCanvas(confirmToolName);
          // An interrupted run leaves the tab in its loading state rather than
          // committing a half-finished payload to the canvas.
          if (completedCanvas && status !== 'interrupted' && canOpenPluginCanvasForChat(chatId)) {
            const completedTool = toolIndex >= 0 ? toolCalls[toolIndex] : toolCalls[toolCalls.length - 1];
            const toolId = normalizeToolId(completedTool?.id);
            const canvas = useCanvasStore.getState();
            const patch = {
              chatId,
              slug: completedCanvas.slug,
              canvasId: completedCanvas.id,
              toolId,
              toolName: confirmToolName,
              title: canvasTabTitle(completedCanvas, completedTool?.input),
              status,
              output: output ?? completedTool?.output,
              ...(status === 'error'
                ? { error: String(eventObj.error || t('加载失败')) }
                : { error: undefined }),
            } as const;
            const targetMatches = canvas.activeView === 'plugin'
              && canvas.pluginTarget?.chatId === chatId
              && canvas.pluginTarget.canvasId === completedCanvas.id
              && (!canvas.pluginTarget.toolId || canvas.pluginTarget.toolId === toolId);
            if (targetMatches) canvas.updatePluginView(patch);
            else canvas.openPluginView(patch);
          }
          // Arrival of choose_design's tool_result = the pick is complete (clicked/skipped/
          // timed out). Whether this stream is live or a replay (replay re-emits design_pick
          // events, but the pick result only shows up in this tool_result), dismiss the pick
          // card on it to prevent zombies.
          if (confirmToolName === 'choose_design') {
            useUIStore.getState().setPendingDesignPick(chatId, null);
          }
          if (Array.isArray(eventObj.citations)) allCitations = [...allCitations, ...(eventObj.citations as CitationItem[])];
          if (enableThinking && !structuredReasoning) {
            // After a tool result the model often keeps reasoning and frequently omits the
            // <think> open tag — re-arm the stripper into the thinking phase; flush the buffer
            // as body text before switching phases.
            if (parseBuffer) {
              if (thinkingPhaseActive) appendThinkContent(parseBuffer, true);
              else appendTextSeg(parseBuffer);
              parseBuffer = '';
            }
            thinkingPhaseActive = true;
          }
          appendOrUpdate(true, allCitations);
          return;
        }

        if (eventType === 'subagent_event') {
          // The sub-agent's internal streaming sub-steps — attached under the call_subagent tool card.
          if (applySubagentEvent(toolCalls, eventObj)) {
            appendOrUpdate(true, allCitations);
          }
          return;
        }

        if (eventType === 'plan_update') {
          // The main agent updated its lightweight plan checklist (update_plan tool).
          // Full-state replace of the plan bar above the input; the agent keeps
          // executing in this same turn, so nothing changes in the message flow.
          //
          // update_plan suppresses its tool_call/tool_result events (no tool card),
          // so the <think> stripper's usual "re-arm after tool result" never fires
          // here — re-arm it now, or the next model iteration's opener-less
          // reasoning (hybrid models omit <think> after tools) leaks into body text.
          if (enableThinking && !structuredReasoning) {
            if (parseBuffer) {
              if (thinkingPhaseActive) appendThinkContent(parseBuffer, true);
              else appendTextSeg(parseBuffer);
              parseBuffer = '';
            }
            thinkingPhaseActive = true;
          }
          const rawSteps = Array.isArray(eventObj.steps) ? eventObj.steps : [];
          const steps = rawSteps
            .map((s) => {
              const o = s as Record<string, unknown>;
              const title = typeof o?.title === 'string' ? o.title.trim() : '';
              const status = o?.status === 'in_progress' || o?.status === 'completed'
                ? o.status : 'pending';
              return title ? { title, status: status as 'pending' | 'in_progress' | 'completed' } : null;
            })
            .filter((s): s is { title: string; status: 'pending' | 'in_progress' | 'completed' } => !!s);
          if (steps.length > 0) {
            useChatStore.getState().setPlanProgress(chatId, {
              source: 'agent',
              title: typeof eventObj.title === 'string' ? eventObj.title : '',
              steps,
              updatedAt: Date.now(),
            });
          }
          return;
        }

        if (eventType === 'ontology_activation') {
          const governance = ensureOntologyGovernance(eventObj);
          const activation = {
            pack_id: typeof eventObj.pack_id === 'string' ? eventObj.pack_id : undefined,
            workflow_id: typeof eventObj.workflow_id === 'string' ? eventObj.workflow_id : undefined,
            workflow_name: typeof eventObj.workflow_name === 'string' ? eventObj.workflow_name : undefined,
            source: typeof eventObj.source === 'string' ? eventObj.source : 'text',
            asset_kind: typeof eventObj.asset_kind === 'string' ? eventObj.asset_kind : undefined,
            asset_id: typeof eventObj.asset_id === 'string' ? eventObj.asset_id : undefined,
            review_level: typeof eventObj.review_level === 'string' ? eventObj.review_level : undefined,
          };
          const activationKey = `${activation.pack_id || ''}:${activation.workflow_id || ''}`;
          if (!governance.activations.some((item) => `${item.pack_id || ''}:${item.workflow_id || ''}` === activationKey)) {
            governance.activations = [...governance.activations, activation];
          }
          appendOrUpdate(true, allCitations);
          return;
        }

        if (eventType === 'ontology_gate') {
          const governance = ensureOntologyGovernance(eventObj);
          governance.gates = [...governance.gates, {
            decision: typeof eventObj.decision === 'string' ? eventObj.decision : 'pass',
            tool_name: typeof eventObj.tool_name === 'string' ? eventObj.tool_name : undefined,
            matched_rule_ids: Array.isArray(eventObj.matched_rule_ids)
              ? eventObj.matched_rule_ids.filter((item): item is string => typeof item === 'string')
              : [],
            violations: Array.isArray(eventObj.violations)
              ? eventObj.violations.filter((item): item is string => typeof item === 'string')
              : [],
            denial_count: typeof eventObj.denial_count === 'number' ? eventObj.denial_count : undefined,
            circuit_breaker: eventObj.circuit_breaker === true,
          }];
          appendOrUpdate(true, allCitations);
          return;
        }

        if (eventType === 'ontology_review') {
          const governance = ensureOntologyGovernance(eventObj);
          const status = typeof eventObj.status === 'string' ? eventObj.status : '';
          const level = typeof eventObj.level === 'string' ? eventObj.level : 'checkpoint';
          const committeeSize = typeof eventObj.committee_size === 'number'
            ? eventObj.committee_size
            : 3;
          governance.review = {
            ...governance.review,
            status: status === 'started' ? 'running' : status,
            level,
            committee_size: committeeSize,
            count: typeof eventObj.review_count === 'number' ? eventObj.review_count : governance.review.count,
            verdict: typeof eventObj.verdict === 'string' ? eventObj.verdict : governance.review.verdict,
            revised: typeof eventObj.revised === 'boolean' ? eventObj.revised : governance.review.revised,
            latency_ms: typeof eventObj.latency_ms === 'number' ? eventObj.latency_ms : governance.review.latency_ms,
            owner: typeof eventObj.review_owner === 'string' ? eventObj.review_owner : governance.review.owner,
            candidate_answer: typeof eventObj.candidate_answer === 'string'
              ? eventObj.candidate_answer
              : governance.review.candidate_answer,
            manual_review: eventObj.manual_review && typeof eventObj.manual_review === 'object'
              ? eventObj.manual_review as typeof governance.review.manual_review
              : governance.review.manual_review,
            violations: Array.isArray(eventObj.violations)
              ? eventObj.violations as Array<Record<string, unknown>>
              : governance.review.violations,
            affected_claims: Array.isArray(eventObj.affected_claims)
              ? eventObj.affected_claims as typeof governance.review.affected_claims
              : governance.review.affected_claims,
            evidence: Array.isArray(eventObj.evidence)
              ? eventObj.evidence.filter((item): item is string => typeof item === 'string')
              : governance.review.evidence,
            feedback: Array.isArray(eventObj.feedback)
              ? eventObj.feedback.filter((item): item is string => typeof item === 'string')
              : governance.review.feedback,
            new_tools: Array.isArray(eventObj.new_tools)
              ? eventObj.new_tools.filter((item): item is string => typeof item === 'string')
              : governance.review.new_tools,
            new_citation_count: typeof eventObj.new_citation_count === 'number'
              ? eventObj.new_citation_count
              : governance.review.new_citation_count,
          };
          if (status === 'started') autoOpenOntologySidebar();
          if (status === 'completed') {
            const candidate = governance.review.candidate_answer || '';
            if (candidate || governance.revision) {
              const revision = ensureOntologyRevision();
              revision.status = 'completed';
              revision.toolPending = false;
              if (candidate) revision.content = candidate;
            }
          }
          appendOrUpdate(true, allCitations);
          return;
        }

        if (eventType === 'thinking' || eventType === 'thought') {
          // Structured reasoning channel (e.g. DeepSeek v4
          // `reasoning_content`): thinking is delivered via this SSE
          // event, not embedded in `content` as <think>...</think>.
          // Disable the embed-tag parser so subsequent content chunks
          // are not treated as buffered thinking.
          if (eventObj.structured_reasoning === true) {
            structuredReasoning = true;
            if (parseBuffer) {
              // An explicit protocol marker means content is always the answer body.
              // This also repairs replay streams where the marker arrives after a
              // buffered content frame.
              appendTextSeg(parseBuffer);
              parseBuffer = '';
            }
            thinkingPhaseActive = false;
            // 后端首轮判定：整轮没有 </think> → 此前"假定为思考"的正文段全部重归正文
            // （修复：无思考输出时整段回答被关进思考块）
            if (reclassifyImplicitThinking()) appendOrUpdate(true);
          }
          if (obj.delta) {
            structuredReasoning = true;
            if (thinkingPhaseActive && parseBuffer) {
              // Buffered during the thinking phase → it is reasoning, not
              // body text. Keep it in the thinking channel. (Previously this
              // flushed to text, leaking reasoning for models that mix the
              // structured channel with inline <think> content.)
              appendThinkContent(parseBuffer, true);
              parseBuffer = '';
            }
            thinkingPhaseActive = false;
          }
          const followsVisibleAnswerText = segments[segments.length - 1]?.type === 'text';
          const thinkContent = (obj.content || obj.text || obj.delta || '') as string;
          if (thinkContent) {
            if (followsVisibleAnswerText) appendLateStructuredThinkContent(thinkContent);
            else appendThinkContent(thinkContent, !!obj.delta);
            appendOrUpdate(true);
          }
          return;
        }

        if (eventType === 'meta') {
          if (typeof eventObj.message_id === 'string') metaMessageId = eventObj.message_id;
          if (typeof eventObj.duration_ms === 'number' && eventObj.duration_ms >= 0) {
            metaDurationMs = eventObj.duration_ms;
          }
          const contextUsage = parseContextUsageSnapshot(eventObj.context_usage);
          if (contextUsage) useChatStore.getState().setContextUsage(chatId, contextUsage);
          compactionPending = eventObj.compaction_pending === true;
          if (Array.isArray(eventObj.citations) && (eventObj.citations as CitationItem[]).length > 0) {
            allCitations = eventObj.citations as CitationItem[];
          }
          if (Array.isArray(eventObj.workspace_files)) {
            metaWorkspaceFiles = (eventObj.workspace_files as unknown[])
              .filter((x): x is string => typeof x === 'string' && x.trim().length > 0);
          }
          if (eventObj.evolution_pending && typeof eventObj.evolution_pending === 'object') {
            const pending = eventObj.evolution_pending as Partial<EvolutionSummary>;
            // A watch token, not something to render: the card stays absent
            // until settlement reports what was actually written.
            evolutionSummary = {
              state: 'pending',
              message_id: typeof pending.message_id === 'string' ? pending.message_id : undefined,
            };
          }
          if (eventObj.ontology_governance && typeof eventObj.ontology_governance === 'object') {
            const persisted = eventObj.ontology_governance as Partial<OntologyGovernanceSummary>;
            const persistedReview = persisted.review && typeof persisted.review === 'object'
              ? persisted.review
              : {};
            const liveRevision = ontologyGovernance?.revision;
            const persistedCandidate = typeof persistedReview.candidate_answer === 'string'
              ? persistedReview.candidate_answer
              : '';
            ontologyGovernance = {
              governance_run_id: persisted.governance_run_id,
              activations: Array.isArray(persisted.activations) ? persisted.activations : [],
              gates: Array.isArray(persisted.gates) ? persisted.gates : [],
              review: persistedReview,
              revision: liveRevision
                ? {
                    ...liveRevision,
                    status: 'completed',
                    content: persistedCandidate || liveRevision.content,
                  }
                : persistedCandidate
                  ? {
                      status: 'completed',
                      content: persistedCandidate,
                      thinking: [],
                      toolCalls: [],
                    }
                  : undefined,
            };
          }
          appendArtifactsToStreamToolCalls(Array.isArray(eventObj.artifacts) ? eventObj.artifacts : []);
          appendOrUpdate(true, allCitations);
          return;
        }

        if (eventType === 'batch_confirm') {
          // batch_runner MCP returned a plan; backend has paused the agent.
          // Open the confirmation modal so the user can review/edit the
          // prompt template before any item executes.
          const planId = typeof eventObj.plan_id === 'string' ? eventObj.plan_id : '';
          if (planId) {
            useBatchStore.getState().setPendingConfirm({
              plan_id: planId,
              total: typeof eventObj.total === 'number' ? eventObj.total : 0,
              source_type: (eventObj.source_type || 'text_list') as
                | 'xlsx' | 'word_files' | 'text_list',
              preview: Array.isArray(eventObj.preview)
                ? (eventObj.preview as Record<string, unknown>[]) : [],
              default_template: typeof eventObj.default_template === 'string'
                ? eventObj.default_template : '',
              placeholder_keys: Array.isArray(eventObj.placeholder_keys)
                ? (eventObj.placeholder_keys as string[]) : [],
              chat_id: typeof eventObj.chat_id === 'string'
                ? eventObj.chat_id : undefined,
              warnings: Array.isArray(eventObj.warnings)
                ? (eventObj.warnings as string[]) : undefined,
            });
          }
          return;
        }

        if (eventType === 'file_confirm') {
          // §13: some tool coroutine has suspended awaiting the user's confirmation of a
          // /myspace write. Show the confirm bar; this SSE stream does **not** end — the user's
          // allow/deny goes via an out-of-band POST /file-confirm, the suspended tool resumes in
          // place, and subsequent tool_result/meta still arrive on this same stream.
          if (eventObj.expired) {
            // The backend's confirmation-wait timeout reclaimed **that** pending item: remove
            // only this one confirm_id from the queue (leave the other queued items alone),
            // otherwise a user clicking much later would inevitably hit a dangling confirm_id error.
            const _cid = String(eventObj.confirm_id ?? '');
            if (_cid) {
              useUIStore.getState().resolvePendingConfirm(chatId, _cid);
              message.info(t('一项「我的空间」写确认已超时取消，如仍需要请重新发起。'));
            }
            return;
          }
          const _info = toFileConfirmInfo(eventObj);
          if (_info.confirmId) useUIStore.getState().enqueuePendingConfirm(chatId, _info);
          return;
        }

        if (eventType === 'design_pick') {
          // Site-design pick-one-of-three: the choose_design tool coroutine suspends awaiting
          // the user's pick. Same mechanism as file_confirm (suspend – out-of-band POST – resume
          // on the original stream); the UI uses a separate pick card.
          applyDesignPickEvent(chatId, eventObj);
          if (eventObj.expired) message.info(t('设计方案选择已超时，助手将自行选择方案继续。'));
          return;
        }

        if (eventType === 'user_question') {
          // ask_user_question suspends the current run. The resident composer
          // replaces the ordinary input until the server resolves this exact
          // request; duplicate replay frames are deduped by request_id.
          const request = toUserQuestionRequest(eventObj);
          if (request.requestId && request.questions.length) {
            useUIStore.getState().enqueuePendingUserQuestion(chatId, request);
          }
          return;
        }

        if (eventType === 'user_question_resolved') {
          // The backend owns the terminal state. A successful answer/cancel
          // POST is only an acknowledgement, so removal happens here (or via
          // the pending-question recovery endpoint after a reconnect).
          const requestId = String(eventObj.request_id ?? '');
          if (requestId) {
            useUIStore.getState().resolvePendingUserQuestion(chatId, requestId);
          }
          if (eventObj.outcome === 'timeout') {
            message.info(t('等待回答已超时，助手将采用稳妥的默认方案继续。'));
          }
          return;
        }

        if (eventType === 'follow_up') {
          if (Array.isArray(eventObj.follow_up_questions) && eventObj.follow_up_questions.length > 0) {
            metaFollowUps = eventObj.follow_up_questions as string[];
            appendOrUpdate(true, allCitations);
          }
          return;
        }

        if (eventType === 'content_replace') {
          const answer = (obj.content || obj.text || '') as string;
          if (parseBuffer) {
            if (thinkingPhaseActive) appendThinkContent(parseBuffer, true);
            parseBuffer = '';
          }
          thinkingPhaseActive = false;
          structuredReasoning = true;
          replaceAnswerText(answer);
          appendOrUpdate(true, allCitations);
          return;
        }

        if (eventType === 'content' || eventType === 'ai_message' || eventType === 'text' || eventType === 'delta') {
          textChunk = (obj.delta || obj.content || obj.text || '') as string;
        }
      }
    } catch (err) {
      if (parsed) throw err;
      textChunk = trimmedPayload;
    }

    if (textChunk) {
      processTextChunk(textChunk);
      appendOrUpdate(true);
    }
  };

  const processSseBlock = (block: string) => {
    if (!block.trim()) return;
    const lines = block.split(/\r?\n/);
    const dataLines: string[] = [];
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith('data:')) dataLines.push(trimmed.slice(5).trim());
    }
    if (dataLines.length === 0) return;
    handleSsePayload(dataLines.join('\n'));
  };

  let thrown: unknown = null;
  _streamActivity.set(chatId, Date.now());
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      // 心跳注释行也算：断连看门狗要的是「传输层还有没有气」，不是「有没有新内容」
      _streamActivity.set(chatId, Date.now());
      sseBuffer += decoder.decode(value, { stream: true });
      const blocks = sseBuffer.split(/\r?\n\r?\n/);
      sseBuffer = blocks.pop() || '';
      for (const block of blocks) {
        processSseBlock(block);
        if (streamEnded) break;
      }
      if (streamEnded) break;
    }
    const tail = sseBuffer.trim();
    if (tail && !streamEnded) processSseBlock(tail);
  } catch (e) {
    if ((e as { name?: string })?.name === 'AbortError') aborted = true;
    else thrown = e;
  }

  // 流已经收尾（正常结束 / 中止 / 异常）→ 摘掉活性标记，别让看门狗对着一条已死的流继续对账
  _streamActivity.delete(chatId);
  // ── Unified wind-down: whether normal end/abort/exception, the bubble must leave the streaming state ──
  // 识图状态必须在这里兜底清掉：中止或异常时那个 status=done 事件永远不会到，
  // 留着的话下一轮会一直显示「图像理解中」。
  useChatStore.getState().setVisionReading(chatId, 0);
  finalizeRunningTools();
  if (parseBuffer) {
    if (thinkingPhaseActive && sawThinkCloseTag) {
      appendThinkContent(parseBuffer, true);
    } else {
      // 整条流从未出现 </think>：残余缓冲是正文，不是思考（结构化 reasoning
      // 模型无思考输出的场景；旧行为会把它并进思考块）
      appendTextSeg(parseBuffer);
    }
    parseBuffer = '';
  }
  // 兜底：老后端/回放流没有首轮协议标记时，流结束仍无任何 think 标签 → 重归类
  reclassifyImplicitThinking();
  deferredThinkingText = restoreDeferredThinkingTextFragment(segments, deferredThinkingText);
  // 收尾统一规则：工具卡/思考块不留在最终答案之后（与历史重建一致，刷新前后不跳变）
  liftTrailingSegmentsAboveFinalText(segments);
  // 收尾直接写 store（不走 appendOrUpdate），所以必须先撤掉还挂着的合并写定时器 ——
  // 否则它会在收尾之后补一帧 isStreaming:true，把气泡永久钉在"生成中"。
  cancelStreamingUpdate();
  const isMd = /\n|```|\*\*|^\s*#\s/m.test(full);
  useChatStore.getState().updateStore((prev) => {
    const c = prev.chats[chatId];
    const msgs = [...(c?.messages || [])];
    const idx = findOwnBubble(msgs);
    if (idx >= 0) {
      msgs[idx] = {
        ...msgs[idx],
        content: full,
        isMarkdown: isMd,
        toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
        thinking: thinking.length > 0 ? thinking : undefined,
        ontologyGovernance,
        segments: segments.length > 0 ? segments : undefined,
        citations: allCitations.length > 0 ? allCitations : undefined,
        followUpQuestions: metaFollowUps.length > 0 ? metaFollowUps : undefined,
        messageId: metaMessageId,
        workspaceFiles: metaWorkspaceFiles,
        isStreaming: false,
        inFlight: undefined,
        durationMs: metaDurationMs ?? (Date.now() - placeholderTs),
      };
    }
    const nextChat: ChatItem = { ...(c as ChatItem), messages: msgs, updatedAt: Date.now() };
    return { chats: { ...prev.chats, [chatId]: nextChat }, order: [chatId, ...(prev.order || []).filter((x) => x !== chatId)] };
  });

  if (compactionPending && !aborted && !thrown) {
    const previousCheckpointId = useChatStore.getState().contextCompactions[chatId]?.checkpointId || '';
    void refreshContextAfterCompaction(
      chatId,
      previousCheckpointId,
      placeholderTs,
      metaMessageId,
    );
  }

  // Settle the plan bar: if this turn produced an agent plan, mark it done so
  // the bar renders as settled (it clears on the next send).
  //
  // 例外 —— 工作流模式把活丢给了后台作业：那一轮在 `run_job(wait=false)` 之后立刻收尾，
  // 计划清单却只走到「提交作业」那一步。照旧标 done 的话，计划栏会在 1/6 步上挂一个绿色
  // 对勾（"已完成"），而作业其实才刚开始跑；此后的进度播报轮不动计划，于是它就永远停在
  // 第 0/1 步——这正是用户看到的"计划模式最后不会更新"。作业还活着就先别收尾，让计划栏
  // 保持真实的进行中状态，由作业跑完那一轮的交付把清单收尾（唤醒提示词已要求收尾）。
  {
    const _pp = useChatStore.getState().planProgress[chatId];
    if (_pp && _pp.source === 'agent' && !_pp.done) {
      const _allSettled = _pp.steps.every(
        (s) => s.status === 'completed' || s.status === 'failed',
      );
      // 只有工作流会话才可能有后台作业（run_job 只在工作流模式下注册），别给普通对话
      // 的每一轮末尾平白加一次请求。
      const _workflowChat = useChatStore.getState().store.chats[chatId]?.workflowChat === true;
      let _jobLive = false;
      if (!_allSettled && _workflowChat) {
        try {
          const jobs = await listChatJobs(chatId);
          _jobLive = jobs.some((j) => j.status === 'running' || j.status === 'pending');
        } catch {
          _jobLive = false; // 查不到就按原来的方式收尾，别让计划栏无限期挂着
        }
      }
      if (!_jobLive) {
        useChatStore.getState().setPlanProgress(chatId, { ..._pp, done: true, updatedAt: Date.now() });
      }
    }
  }

  if (thrown) throw thrown;

  return { full, placeholderTs, metaMessageId, metaFollowUps, aborted, queuedRun };
}
