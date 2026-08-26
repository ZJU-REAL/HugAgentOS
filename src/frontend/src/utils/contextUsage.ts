import { t } from '../i18n';
import type { ChatMessage, ContextCompactionState, ToolCall } from '../types';

/**
 * Client-side context-usage estimation.
 *
 * The backend does not expose a live per-conversation token counter, and we
 * intentionally avoid bundling a heavy tokenizer (tiktoken) into the web app.
 * Instead we approximate token counts from character composition, which is
 * accurate enough for a "how full is my context window" gauge:
 *   - CJK / full-width chars ≈ 1 token each
 *   - other chars           ≈ 1 token per ~4 chars
 * All figures shown to the user are labelled as estimates (预估).
 */

// Matches CJK ideographs, kana, hangul and full-width forms — the ranges that
// tokenize to roughly one token per character.
const CJK_RE = /[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯＀-／０-￯]/g;

/** Rough token estimate for a piece of mixed CN/EN text. */
export function estimateTokens(text: string): number {
  if (!text) return 0;
  const cjk = (text.match(CJK_RE) || []).length;
  const other = text.length - cjk;
  return Math.ceil(cjk + other / 4);
}

function serialize(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

// ── Context-window lookup ────────────────────────────────────────────────
// Preferred source is the real per-model `context_length` from the backend
// (ModelProvider.extra_config.context_length, surfaced via
// `/v1/models/capabilities`). When that is missing/0 we infer the window from
// the model name/family, and only then fall back to a conservative default.
// Extend this table as new providers/models are onboarded.
const CONTEXT_WINDOWS: Array<{ test: RegExp; window: number }> = [
  { test: /claude/i, window: 200_000 },
  { test: /gpt-4\.1|gpt-4o|gpt-4-turbo|o1|o3|o4-mini/i, window: 128_000 },
  { test: /gpt-4-32k/i, window: 32_768 },
  { test: /gpt-4/i, window: 8_192 },
  { test: /gpt-3\.5/i, window: 16_385 },
  { test: /gemini.*(1\.5|2\.0|2\.5)/i, window: 1_000_000 },
  { test: /gemini/i, window: 1_000_000 },
  { test: /deepseek/i, window: 64_000 },
  { test: /(qwen|通义).*(max|plus|turbo|long)/i, window: 128_000 },
  { test: /qwen|通义/i, window: 32_768 },
  { test: /kimi|moonshot/i, window: 200_000 },
  { test: /glm-4|chatglm|智谱/i, window: 128_000 },
  { test: /doubao|豆包/i, window: 128_000 },
  { test: /ernie|文心/i, window: 128_000 },
  { test: /yi-/i, window: 200_000 },
];

/** Default context window when the model family is unknown. */
export const DEFAULT_CONTEXT_WINDOW = 128_000;

export interface ContextWindowModel {
  model_name?: string;
  display_name?: string;
  provider?: string;
  /** Real context window (tokens) from backend config; preferred when > 0. */
  context_length?: number;
}

/**
 * Context-window size (in tokens) for a selectable model.
 *
 * Resolution order: the model's real `context_length` (backend config) →
 * `fallbackWindow` (e.g. the main model's window from capabilities) →
 * model-name heuristic → conservative default.
 */
export function getContextWindow(model?: ContextWindowModel | null, fallbackWindow = 0): number {
  if (model?.context_length && model.context_length > 0) return model.context_length;
  if (fallbackWindow && fallbackWindow > 0) return fallbackWindow;
  if (model) {
    const hay = `${model.model_name || ''} ${model.display_name || ''}`.toLowerCase();
    for (const { test, window } of CONTEXT_WINDOWS) {
      if (test.test(hay)) return window;
    }
  }
  return DEFAULT_CONTEXT_WINDOW;
}

// ── Auto-detection wording ───────────────────────────────────────────────
// The backend (core/llm/providers/context_probe.py) can read a model's real window off
// the vendor instead of making the operator look it up. It reports how it learned the
// number, and the wording below keeps that visible: a window the upstream published is
// not the same claim as one inferred from the model's name.

export interface ContextProbeSummary {
  context_length: number;
  source_label?: string;
  confidence?: string;
  detail?: string;
  notes?: string[];
}

/** One line explaining a detection result, including how much to trust it. */
export function describeContextProbe(result: ContextProbeSummary): string {
  if (!result.context_length) {
    const notes = (result.notes || []).join('；');
    return notes
      ? t('未探测到上下文窗口，请手工填写。探测过程：{notes}', { notes })
      : t('未探测到上下文窗口，请手工填写。');
  }
  const base = t('已探测到 {n} token（来源：{src}）', {
    n: String(result.context_length),
    src: result.source_label || '',
  });
  if (result.confidence === 'low') {
    return `${base}｜${t('该值按模型名推断，并非上游自报，建议核对后再保存。')}`;
  }
  if (result.confidence === 'medium') {
    return `${base}｜${t('该值来自上游报错信息，建议核对后再保存。')}`;
  }
  return base;
}

// ── Breakdown computation ────────────────────────────────────────────────

export interface ContextBreakdown {
  /** User + assistant message text ("提示词/对话"). */
  messages: number;
  /** Tool-call names, inputs and outputs ("工具调用"). */
  tools: number;
  /** Model reasoning / thinking blocks ("思考过程"). */
  thinking: number;
  /** Attached / staged files ("文件"). */
  files: number;
  /** Fixed baseline for the system prompt + tool schema we cannot see. */
  system: number;
  /** The current unsent composer draft ("当前输入"). */
  input: number;
  /** Sum of all buckets. */
  total: number;
}

// Rough baseline for the (client-invisible) system prompt + tool definitions.
export const SYSTEM_BASE_TOKENS = 1_200;
// Per historical attachment: the parsed file content is not retained on the
// client after send, so we estimate a nominal per-file contribution.
export const ATTACHMENT_EST_TOKENS = 800;
/** Fallback matching the backend's bounded first-turn attachment preview. */
export const DEFAULT_ATTACHMENT_PREVIEW_CHARS = 5_000;

export interface StagedFileEstimate {
  name?: string;
  type?: string;
  size?: number;
}

function isImageFile(file: StagedFileEstimate): boolean {
  if ((file.type || '').toLowerCase().startsWith('image/')) return true;
  return /\.(png|jpe?g|gif|webp|bmp|svg|ico)$/i.test(file.name || '');
}

/** Estimate what the backend will inject, never the raw binary transfer size. */
export function estimateStagedFileTokens(
  file: StagedFileEstimate,
  previewChars = DEFAULT_ATTACHMENT_PREVIEW_CHARS,
): number {
  if (isImageFile(file)) return ATTACHMENT_EST_TOKENS;
  const previewCap = Math.max(1, previewChars || DEFAULT_ATTACHMENT_PREVIEW_CHARS);
  // Parsed text length/content is unknown before the backend reads the file.
  // Use its explicit preview budget; binary byte size is not a token proxy.
  return previewCap;
}

interface MsgTokens {
  messages: number;
  tools: number;
  thinking: number;
  files: number;
}

// Message objects are immutable snapshots (Zustand replaces them on update),
// so we can memoize per-object by identity and only recompute the one message
// that changed during streaming.
const msgCache = new WeakMap<ChatMessage, MsgTokens>();

function tokensForToolCall(tc: ToolCall): number {
  let n = estimateTokens(tc.name || '');
  n += estimateTokens(serialize(tc.input));
  n += estimateTokens(serialize(tc.output));
  if (tc.subSteps) {
    for (const s of tc.subSteps) {
      n += estimateTokens(s.text || '');
      n += estimateTokens(serialize(s.input));
      n += estimateTokens(serialize(s.output));
    }
  }
  return n;
}

function tokensForMessage(m: ChatMessage): MsgTokens {
  const cached = msgCache.get(m);
  if (cached) return cached;

  let tools = 0;
  let thinking = 0;
  let files = 0;
  const messages = estimateTokens(m.content || '');
  if (m.toolCalls) {
    for (const tc of m.toolCalls) tools += tokensForToolCall(tc);
  }
  if (m.thinking) {
    for (const th of m.thinking) thinking += estimateTokens(th.content || '');
  }
  if (m.attachments && m.attachments.length) {
    files += m.attachments.length * ATTACHMENT_EST_TOKENS;
  }

  const result: MsgTokens = { messages, tools, thinking, files };
  // Don't cache a still-streaming message: its content keeps growing while the
  // object identity may be reused across deltas in some update paths.
  if (!m.isStreaming) msgCache.set(m, result);
  return result;
}

export interface BreakdownOptions {
  /** Current unsent composer draft. */
  draft?: string;
  /** Files staged in the composer but not yet sent. */
  stagedFiles?: StagedFileEstimate[];
  /** Backend's per-file automatic preview character budget. */
  attachmentPreviewChars?: number;
  /**
   * Tokens occupied by the system prompt + tool/skill definitions. Prefer the
   * backend's real reserve (capabilities.system_prompt_tokens); falls back to
   * SYSTEM_BASE_TOKENS when not provided.
   */
  systemTokens?: number;
  /** Latest server-side compaction checkpoint, when this chat was compacted. */
  compaction?: ContextCompactionState | null;
}

/** Parse the snake_case checkpoint projection returned by the API/SSE. */
export function parseContextCompactionState(value: unknown): ContextCompactionState | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  const checkpointId = typeof raw.checkpoint_id === 'string' ? raw.checkpoint_id : '';
  const checkpointCreatedAt = typeof raw.checkpoint_created_at === 'string'
    ? Date.parse(raw.checkpoint_created_at)
    : Number.NaN;
  const coveredMessageCount = Number(raw.covered_message_count);
  const replacementTokens = Number(raw.replacement_tokens);
  if (
    !checkpointId
    || !Number.isFinite(coveredMessageCount)
    || coveredMessageCount < 0
    || !Number.isFinite(replacementTokens)
    || replacementTokens < 0
  ) {
    return null;
  }
  return {
    checkpointId,
    checkpointTs: Number.isNaN(checkpointCreatedAt) ? 0 : checkpointCreatedAt,
    coveredThroughMessageId: typeof raw.covered_through_message_id === 'string'
      ? raw.covered_through_message_id
      : undefined,
    coveredMessageCount: Math.floor(coveredMessageCount),
    replacementTokens: Math.floor(replacementTokens),
  };
}

function messagesAfterCompaction(
  messages: ChatMessage[],
  compaction: ContextCompactionState,
): ChatMessage[] {
  // The persisted boundary ID is the strongest signal and also works when
  // client/server clocks or timezone serialization differ.
  if (compaction.coveredThroughMessageId) {
    const boundary = messages.findIndex(
      (m) => m.messageId === compaction.coveredThroughMessageId,
    );
    if (boundary >= 0) return messages.slice(boundary + 1);
  }
  // The API supplies the count in the same visible ordering as /messages.
  if (messages.length >= compaction.coveredMessageCount) {
    return messages.slice(compaction.coveredMessageCount);
  }
  // Partial-history fallback while a multi-page load is still in progress.
  if (compaction.checkpointTs > 0) {
    return messages.filter((m) => m.ts > compaction.checkpointTs);
  }
  return [];
}

/** Compute the estimated context breakdown for a conversation. */
export function computeContextBreakdown(
  messages: ChatMessage[] | undefined | null,
  opts: BreakdownOptions = {},
): ContextBreakdown {
  let messagesTok = opts.compaction?.replacementTokens || 0;
  let toolsTok = 0;
  let thinkingTok = 0;
  let filesTok = 0;

  const visibleMessages = messages || [];
  const activeMessages = opts.compaction
    ? messagesAfterCompaction(visibleMessages, opts.compaction)
    : visibleMessages;
  for (const m of activeMessages) {
    const t = tokensForMessage(m);
    messagesTok += t.messages;
    toolsTok += t.tools;
    thinkingTok += t.thinking;
    filesTok += t.files;
  }

  // Staged files are uploaded by file_id. The backend injects only a bounded
  // preview, so raw binary size must never be treated as prompt size.
  for (const file of opts.stagedFiles || []) {
    filesTok += estimateStagedFileTokens(file, opts.attachmentPreviewChars);
  }

  const inputTok = estimateTokens(opts.draft || '');
  const systemTok = opts.systemTokens && opts.systemTokens > 0
    ? opts.systemTokens
    : SYSTEM_BASE_TOKENS;
  const total = messagesTok + toolsTok + thinkingTok + filesTok + systemTok + inputTok;

  return {
    messages: messagesTok,
    tools: toolsTok,
    thinking: thinkingTok,
    files: filesTok,
    system: systemTok,
    input: inputTok,
    total,
  };
}

/** Format a token count compactly, e.g. 1234 → "1.2k", 128000 → "128k". */
export function formatTokens(n: number): string {
  if (n < 1000) return String(Math.round(n));
  if (n < 10_000) return `${(n / 1000).toFixed(1)}k`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}
