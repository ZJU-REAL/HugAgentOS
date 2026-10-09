import {
getChatContextState,
toDesignPickInfo
} from '../../api';
import { t } from '../../i18n';
import { readText,resolveText } from '../../plugin-ui';
import { panelFromPath } from '../../routing/navigation';
import { writeLocal } from '../../storage';
import { useAgentStore,useCatalogStore,useChatStore,usePluginStore,useUIStore } from '../../stores';
import { usePluginUiStore,type CanvasTarget } from '../../stores/pluginUiStore';
import type { ChatMessage,SubagentStep,ToolCall } from '../../types';
import { stripMcpToolPrefix } from '../../utils/constants';
import {
isCompactionCheckpointForRun,
parseContextCompactionState,
parseContextUsageSnapshot,
} from '../../utils/contextUsage';
import { type QueuedRunHandoff } from '../../utils/streamHandoff';
import {
appendSubagentStepDelta,
finishSubagentToolCall
} from '../../utils/streamSegments';
import { resolveSubagentParentIndex } from '../../utils/toolMatching';
import { refreshTargetForTool } from '../../utils/toolRefresh';
import { isMobileViewport } from '../useIsMobileViewport';
export const _streamActivity = new Map<string, number>();
export function getStreamActivityTs(chatId: string): number {
  return _streamActivity.get(chatId) || 0;
}
export const _seenRuns = new Set<string>();
export function hasStreamedRun(runId: string): boolean {
  return _seenRuns.has(runId);
}
export const COMPACTION_REFRESH_DELAYS_MS = [500, 1_000, 2_000, 4_000, 8_000, 12_000, 16_000, 20_000];
export async function refreshContextAfterCompaction(
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
export const CANCELLED_RUNS_KEY = 'hugagent_ui_cancelled_runs_v1';
export const CANCELLED_RUNS_MAX = 100;
export const _cancelledRuns = new Set<string>();
export function loadCancelledRuns(): Set<string> {
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
export function maybeRefreshCatalogAfterTool(toolName: string, status: string): void {
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
export function canAutoOpenCanvas(): boolean {
  return !isMobileViewport();
}
export function canOpenPluginCanvasForChat(chatId: string): boolean {
  return useChatStore.getState().currentChatId === chatId
    && panelFromPath() === 'chat'
    && canAutoOpenCanvas();
}
export function findAutoCanvas(toolName: string | undefined): CanvasTarget | null {
  if (!toolName) return null;
  return usePluginUiStore.getState().findCanvasTargetForTool(toolName);
}
export function canvasTabTitle(target: CanvasTarget, toolInput: unknown): string {
  const fromInput = target.titleFromInput ? readText(toolInput, target.titleFromInput) : '';
  return fromInput || resolveText(target.title);
}
export function applyDesignPickEvent(chatId: string, obj: Record<string, unknown>) {
  const ui = useUIStore.getState();
  if (obj.expired) {
    ui.setPendingDesignPick(chatId, null);
    return;
  }
  const pick = toDesignPickInfo(obj);
  if (pick.confirmId && pick.options.length) ui.setPendingDesignPick(chatId, pick);
}
export function applySubagentEvent(toolCalls: ToolCall[], eo: Record<string, unknown>): boolean {
  const norm = (v: unknown): string => (v == null ? '' : String(v));
  const parentId = norm(eo.parent_tool_id);
  // 事件自报父卡片工具名时按它回退（批量作业的进度贴的是 run_job，不是 call_subagent）；
  // 只有事件没带 parent_tool_id 时才轮得到名字——见 utils/toolMatching。
  const parentName = norm(eo.parent_tool_name) || 'call_subagent';
  const idx = resolveSubagentParentIndex(toolCalls, parentId || undefined, parentName);
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
  const updated = { ...parent, subSteps: steps, ...(agentName ? { subagentName: agentName } : {}) };
  const terminalStatus = norm(eo.status) === 'cancelled'
    ? 'interrupted' : eo.ok === true ? 'success' : 'error';
  toolCalls[idx] = subType === 'end' ? finishSubagentToolCall(updated, terminalStatus) : updated;
  return true;
}
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
  signal?: AbortSignal;
  /** Thinking mode (chatMode !== 'fast'): determines the <think> stripper's initial phase and re-arming after tools */
  enableThinking: boolean;
  /** Placeholder notice shown until the first real event arrives (confirm-then-continue scenarios etc.), never persisted */
  pendingNotice?: string;
  /** Path-specific event preprocessing (the autonomous loop's loop_*). Return true = handled, skip built-in dispatch. */
  onEvent?: (ev: Record<string, unknown>, api: ChatStreamApi) => boolean;
  /** Local identity for admission; a run snapshot supplies the resume state. */
  seedFrom?: ChatMessage;
}
export interface ChatStreamOutcome {
  /** Final body text (excluding thinking) */
  full: string;
  /** A task verdict was received, rather than merely a closed connection. */
  settled: boolean;
  /** The assistant bubble's identity (follow-up polling etc. locate the message by it) */
  bubbleUid: string;
  /** Backend message_id carried back by the meta event */
  metaMessageId?: string;
  /** Follow-up questions delivered directly within the stream */
  metaFollowUps: string[];
  /** The connection was aborted; only an explicit task cancel settles its message. */
  aborted: boolean;
  /** A durable queued-input handoff committed by the backend at this run's boundary. */
  queuedRun?: QueuedRunHandoff;
}