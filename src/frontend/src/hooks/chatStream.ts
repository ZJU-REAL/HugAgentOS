import { message } from 'antd';
import { t } from '../i18n';
import { toFileConfirmInfo, toDesignPickInfo } from '../api';
import { normalizeArtifactOutput } from '../utils/fileParser';
import { stripMcpToolPrefix } from '../utils/constants';
import { useChatStore, useCatalogStore, useUIStore, useBatchStore, useCanvasStore } from '../stores';
import type { ChatItem, ChatMessage, CitationItem, MessageSegment, SubagentStep, ToolCall } from '../types';

/**
 * Unified chat SSE stream processor (single source of truth).
 *
 * Send, regenerate/edit-resend, reconnect replay (follow), batch cancel-and-resume, and
 * autonomous loop (loop start/resume/follow) **all** go through this one processor: the same
 * event vocabulary (content/thinking/tool_call/tool_result/meta/…), the same <think> stripping
 * state machine, and the same bubble rendering pipeline.
 *
 * Path-specific events (e.g. the autonomous loop's loop_started/loop_plan/…) are intercepted via
 * the `onEvent` hook before built-in handling — the hook only owns its own extra UI (plan bar
 * etc.); bubble rendering is still done uniformly by this processor. Copying this file's
 * reduction logic for new scenarios is forbidden.
 */

/** Tools in the skill-manager plugin that mutate "my skill library" — after the agent calls
 *  them the capability catalog must be refreshed, otherwise the frontend skill list stays on
 *  stale data (no visible change after create/install/delete). search/list/submit are read-only
 *  or don't change the list, so they don't trigger it. */
const SKILL_LIBRARY_MUTATING_TOOLS = ['register_skill', 'install_from_marketplace', 'delete_skill'];

function maybeRefreshCatalogAfterTool(toolName: string, status: string): void {
  if (status !== 'success') return;
  const name = stripMcpToolPrefix(toolName || '');
  if (SKILL_LIBRARY_MUTATING_TOOLS.some((n) => name.includes(n))) {
    void useCatalogStore.getState().fetchCatalog();
  }
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
 * Handle one `subagent_event`: attach the sub-agent's internal thinking/tool_call/tool_result/
 * content sub-steps under the call_subagent tool card that spawned it.
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
  let idx = -1;
  if (parentId) idx = toolCalls.findIndex((t) => norm(t?.id) === parentId);
  if (idx < 0) {
    for (let i = toolCalls.length - 1; i >= 0; i--) {
      if (toolCalls[i]?.name === 'call_subagent') { idx = i; break; }
    }
  }
  if (idx < 0) return false;

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
  } else if (subType === 'tool_result') {
    const status: SubagentStep['status'] = norm(eo.status) === 'error' ? 'error' : 'success';
    const patch: Partial<SubagentStep> = { status };
    if (eo.output !== null && eo.output !== undefined) patch.output = eo.output;
    upsertToolStep(norm(eo.tool_id), norm(eo.tool_name), patch, status);
  } else if (subType === 'thinking' || subType === 'content') {
    const delta = norm(eo.delta);
    if (delta) {
      const last = steps[steps.length - 1];
      if (last && last.kind === subType) {
        steps[steps.length - 1] = { ...last, text: (last.text || '') + delta };
      } else {
        steps.push({ kind: subType, text: delta });
      }
    }
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
  /** The to-be-planned task from an enter_plan_mode redirect (consumed only by the send path) */
  pendingPlanRedirect: string | null;
  /** Stream aborted by the user (AbortError) — the bubble has already wound down normally */
  aborted: boolean;
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
  let metaMessageId: string | undefined;
  let metaFollowUps: string[] = [];
  let allCitations: CitationItem[] = [];
  // Workspace allowlist from the meta event. `null` means the agent
  // didn't pin (legacy behavior); an array means filter artifact cards
  // to only those file_ids.
  let metaWorkspaceFiles: string[] | null = null;
  let parseBuffer = '';
  let toolPending = false;
  let pendingPlanRedirect: string | null = null;
  let aborted = false;

  // ── <think>...</think> stripping state machine ──
  // Many models (qwen3 / DeepSeek family) inline their reasoning in the content stream. The
  // stripper cuts it into separate thinking segments so the bubble renders a collapsible
  // thinking block instead of visible body text.
  let thinkingPhaseActive = enableThinking;
  // Once a structured reasoning event is observed (e.g. DeepSeek v4 `reasoning_content`), pin
  // the stripper's phase to body — from then on content is no longer treated as buffered thinking.
  let structuredReasoning = false;

  const getPartialTagLen = (text: string, tag: string): number => {
    for (let len = Math.min(tag.length - 1, text.length); len >= 1; len--) {
      if (tag.startsWith(text.slice(text.length - len))) return len;
    }
    return 0;
  };

  const appendThinkContent = (content: string, isDelta: boolean) => {
    if (!content) return;
    const lastSeg = segments[segments.length - 1];
    const lastThink = isDelta && lastSeg?.type === 'thinking' ? lastSeg : null;
    if (lastThink) {
      lastThink.content = (lastThink.content || '') + content;
      if (thinking.length > 0) thinking[thinking.length - 1].content += content;
      else thinking.push({ content, timestamp: Date.now() });
    } else {
      segments.push({ type: 'thinking', content });
      thinking.push({ content, timestamp: Date.now() });
    }
  };

  const appendTextSeg = (text: string) => {
    if (!text) return;
    full += text;
    const last = segments[segments.length - 1];
    if (last && last.type === 'text') last.content = (last.content || '') + text;
    else segments.push({ type: 'text', content: text });
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
            parseBuffer = parseBuffer.slice(safeLen);
          }
          break;
        }
        if (closeIdx > 0) appendThinkContent(parseBuffer.slice(0, closeIdx), true);
        parseBuffer = parseBuffer.slice(closeIdx + 8);
        thinkingPhaseActive = false;
      } else {
        const openIdx = parseBuffer.indexOf('<think>');
        const closeIdx = parseBuffer.indexOf('</think>');
        // Orphan close tag (no paired <think>): the model omitted the open tag (common after
        // tool calls, in fast mode, or after a structured reasoning event pinned the phase to
        // body). Everything before the close tag is reasoning, not body text.
        if (closeIdx >= 0 && (openIdx === -1 || closeIdx < openIdx)) {
          if (closeIdx > 0) appendThinkContent(parseBuffer.slice(0, closeIdx), true);
          parseBuffer = parseBuffer.slice(closeIdx + 8);
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
    const byNameIndex = findLastRunningToolIndex(eventToolName);
    if (byNameIndex >= 0) return byNameIndex;
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
    if (latestHtml && latestHtml.url) {
      const canvas = useCanvasStore.getState();
      // Don't steal focus from a different file the user is actively viewing —
      // only auto-open if Canvas is closed or already showing this same artifact.
      if (!canvas.isOpen || !canvas.artifact || canvas.artifact.file_id === latestHtml.file_id) {
        canvas.openCanvas({ ...latestHtml, chat_id: chatId });
      }
    }
    return changed;
  };

  const placeholderTs = Date.now();
  const appendOrUpdate = (streaming: boolean, cits?: CitationItem[]) => {
    useChatStore.getState().updateStore((prev) => {
      const c = prev.chats[chatId];
      const msgs = [...(c?.messages || [])];
      const last = msgs[msgs.length - 1];
      // While the model hasn't produced any real content yet (MiniMax may buffer the whole
      // turn), show the placeholder notice instead of an empty bubble. The placeholder never
      // enters full/segments, so it is never persisted.
      const body = (
        !full && streaming && toolCalls.length === 0 && thinking.length === 0
          ? (pendingNotice || '')
          : full
      );
      const isMd = streaming && (body.includes('\n') || body.includes('```') || body.includes('**') || /^\s*#\s/m.test(body));
      const updatedMsg: Partial<ChatMessage> & { content: string; isMarkdown: boolean; isStreaming: boolean } = {
        content: body,
        isMarkdown: isMd,
        toolCalls: toolCalls.length > 0 ? [...toolCalls] : undefined,
        thinking: thinking.length > 0 ? [...thinking] : undefined,
        segments: segments.length > 0 ? [...segments] : undefined,
        isStreaming: streaming,
        toolPending: streaming && toolPending,
        // Persisted activity stamp — anchors the "正在准备调用工具…" timer so
        // it survives a session switch / refresh remount (see useStallDetector).
        lastActivityTs: Date.now(),
      };
      if (cits !== undefined) updatedMsg.citations = cits.length > 0 ? cits : undefined;
      if (metaFollowUps.length > 0) updatedMsg.followUpQuestions = metaFollowUps;

      if (last?.role === 'assistant' && last.ts === placeholderTs) {
        msgs[msgs.length - 1] = { ...last, ...updatedMsg };
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

  const hookApi: ChatStreamApi = {
    appendText: (txt: string) => appendTextSeg(txt),
    hasText: () => full.length > 0,
    refresh: () => appendOrUpdate(true, allCitations),
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
            useChatStore.getState().setActiveRun(chatId, { runId, messageId });
          }
          return;
        }
        if (eventType === 'compaction_notice') {
          // Earlier context was compacted in the background after the previous turn ended;
          // the backend notifies once in this turn's first frame
          // → ChatArea shows a dismissible notice bar
          useChatStore.getState().setCompactionNotice(chatId);
          return;
        }
        if (eventType === 'end') {
          if (finalizeRunningTools()) appendOrUpdate(true);
          streamEnded = true;
          return;
        }
        if (eventType === 'error') throw new Error(typeof obj.error === 'string' ? obj.error : t('流式响应异常'));

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

        if (eventType === 'tool_use' || eventType === 'tool_call' || eventType === 'tool_start') {
          const eventToolId = getEventToolId(eventObj);
          const existingIndex = eventToolId ? toolCalls.findIndex((tool) => normalizeToolId(tool.id) === eventToolId) : -1;
          const toolInput = eventObj.input ?? eventObj.args ?? eventObj.tool_args ?? eventObj.arguments;
          const rawName = getEventToolRawName(eventObj);
          const displayName = getEventToolDisplayName(eventObj);
          if (existingIndex >= 0) {
            const existing = toolCalls[existingIndex];
            toolCalls[existingIndex] = { ...existing, name: rawName || existing.name, displayName: displayName || existing.displayName, input: toolInput ?? existing.input, status: 'running' };
          } else {
            toolCalls.push({ id: eventToolId || `tool_${Date.now()}_${toolCalls.length}`, name: rawName || t('工具调用'), displayName, input: toolInput, status: 'running', timestamp: Date.now() });
            segments.push({ type: 'tool', toolIndex: toolCalls.length - 1 });
          }
          appendOrUpdate(true);
          return;
        }

        if (eventType === 'tool_result' || eventType === 'tool_end') {
          const toolIndex = findToolCallIndex(eventObj);
          const status: ToolCall['status'] = obj.error ? 'error' : 'success';
          const output = eventObj.output ?? eventObj.result;

          let resultDisplayName: string | undefined;
          if (typeof obj.subagent_name === 'string' && obj.subagent_name.trim()) {
            resultDisplayName = t('调用子智能体：{name}', { name: obj.subagent_name.trim() });
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
              appendTextSeg(parseBuffer);
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

        if (eventType === 'plan_redirect') {
          // The main agent judged the task complex and called enter_plan_mode → the backend
          // aborts this turn and sends the to-be-planned task. Only record the task text and
          // switch into plan mode after this stream ends cleanly, to avoid starting a second
          // request mid-stream-processing.
          const _task = typeof eventObj.task_description === 'string'
            ? eventObj.task_description.trim() : '';
          if (_task) pendingPlanRedirect = _task;
          return;
        }

        if (eventType === 'thinking' || eventType === 'thought') {
          // Structured reasoning channel (e.g. DeepSeek v4
          // `reasoning_content`): thinking is delivered via this SSE
          // event, not embedded in `content` as <think>...</think>.
          // Disable the embed-tag parser so subsequent content chunks
          // are not treated as buffered thinking.
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
          const thinkContent = (obj.content || obj.text || obj.delta || '') as string;
          if (thinkContent) {
            appendThinkContent(thinkContent, !!obj.delta);
            appendOrUpdate(true);
          }
          return;
        }

        if (eventType === 'meta') {
          if (typeof eventObj.message_id === 'string') metaMessageId = eventObj.message_id;
          if (Array.isArray(eventObj.citations) && (eventObj.citations as CitationItem[]).length > 0) {
            allCitations = eventObj.citations as CitationItem[];
          }
          if (Array.isArray(eventObj.workspace_files)) {
            metaWorkspaceFiles = (eventObj.workspace_files as unknown[])
              .filter((x): x is string => typeof x === 'string' && x.trim().length > 0);
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

        if (eventType === 'follow_up') {
          if (Array.isArray(eventObj.follow_up_questions) && eventObj.follow_up_questions.length > 0) {
            metaFollowUps = eventObj.follow_up_questions as string[];
            appendOrUpdate(true, allCitations);
          }
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
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
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

  // ── Unified wind-down: whether normal end/abort/exception, the bubble must leave the streaming state ──
  finalizeRunningTools();
  if (parseBuffer) {
    if (thinkingPhaseActive) {
      appendThinkContent(parseBuffer, true);
    } else {
      appendTextSeg(parseBuffer);
    }
    parseBuffer = '';
  }
  const isMd = /\n|```|\*\*|^\s*#\s/m.test(full);
  useChatStore.getState().updateStore((prev) => {
    const c = prev.chats[chatId];
    const msgs = [...(c?.messages || [])];
    const last = msgs[msgs.length - 1];
    if (last?.role === 'assistant' && last.ts === placeholderTs) {
      msgs[msgs.length - 1] = {
        ...last,
        content: full,
        isMarkdown: isMd,
        toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
        thinking: thinking.length > 0 ? thinking : undefined,
        segments: segments.length > 0 ? segments : undefined,
        citations: allCitations.length > 0 ? allCitations : undefined,
        followUpQuestions: metaFollowUps.length > 0 ? metaFollowUps : undefined,
        messageId: metaMessageId,
        workspaceFiles: metaWorkspaceFiles,
        isStreaming: false,
        durationMs: Date.now() - placeholderTs,
      };
    }
    const nextChat: ChatItem = { ...(c as ChatItem), messages: msgs, updatedAt: Date.now() };
    return { chats: { ...prev.chats, [chatId]: nextChat }, order: [chatId, ...(prev.order || []).filter((x) => x !== chatId)] };
  });

  if (thrown) throw thrown;

  return { full, placeholderTs, metaMessageId, metaFollowUps, pendingPlanRedirect, aborted };
}
