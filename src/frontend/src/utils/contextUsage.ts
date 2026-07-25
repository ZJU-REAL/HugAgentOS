import type { ChatMessage, ToolCall } from '../types';

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
  /** Total bytes of files staged in the composer but not yet sent. */
  stagedBytes?: number;
  /** Count of imported-space files staged in the composer. */
  stagedFileCount?: number;
  /**
   * Tokens occupied by the system prompt + tool/skill definitions. Prefer the
   * backend's real reserve (capabilities.system_prompt_tokens); falls back to
   * SYSTEM_BASE_TOKENS when not provided.
   */
  systemTokens?: number;
}

/** Compute the estimated context breakdown for a conversation. */
export function computeContextBreakdown(
  messages: ChatMessage[] | undefined | null,
  opts: BreakdownOptions = {},
): ContextBreakdown {
  let messagesTok = 0;
  let toolsTok = 0;
  let thinkingTok = 0;
  let filesTok = 0;

  for (const m of messages || []) {
    const t = tokensForMessage(m);
    messagesTok += t.messages;
    toolsTok += t.tools;
    thinkingTok += t.thinking;
    filesTok += t.files;
  }

  // Staged (not-yet-sent) files: estimate ~4 bytes/token from raw size, plus a
  // nominal estimate for imported-space files whose size we don't have here.
  if (opts.stagedBytes) filesTok += Math.ceil(opts.stagedBytes / 4);
  if (opts.stagedFileCount) filesTok += opts.stagedFileCount * ATTACHMENT_EST_TOKENS;

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
